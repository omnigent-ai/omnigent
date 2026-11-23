"""Tests for the authenticated Databricks Managed Memory REST client."""

from __future__ import annotations

import builtins
import sys
import types
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, cast

import pytest

from omnigent.stores.memory_store.databricks_client import (
    DatabricksManagedMemoryClient,
)

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service.agentkit import ManagedMemoryEntry


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
        headers: dict[str, str] | None = None,
    ) -> object:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": query,
                "headers": headers,
            }
        )
        return self.response


class _RecordingWorkspaceClient:
    def __init__(self) -> None:
        self.api_client = _RecordingApiClient()
        self.config = types.SimpleNamespace(workspace_id="1234567890")
        self.agent_kit = types.SimpleNamespace(
            get_memory_store=lambda name: types.SimpleNamespace(name=name)
        )


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

    assert client.get_store().name == "memory-stores/support-memory"
    assert client.get_effective_permission() == {"ok": True}

    assert api_client.calls == [
        {
            "method": "GET",
            "path": "/api/2.0/agents/memory-stores/support-memory/permissions/self",
            "query": None,
            "headers": {
                "Accept": "application/json",
                "X-Databricks-Workspace-Id": "1234567890",
            },
        },
    ]


def test_entry_requests_use_canonical_shapes(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client

    client.list_entries(
        "alice",
        path_prefix="/preferences",
        page_size=25,
        page_token="next",
        session_id="session-one",
    )
    client.list_entries(
        "alice",
        path="/preferences",
    )

    base = "/api/2.0/agents/memory-stores/support-memory/entries"
    headers = {
        "Accept": "application/json",
        "X-Databricks-Workspace-Id": "1234567890",
    }
    assert api_client.calls == [
        {
            "method": "GET",
            "path": base,
            "query": {
                "actor_id": "alice",
                "path_prefix": "/preferences",
                "page_size": 25,
                "page_token": "next",
                "session_id": "session-one",
            },
            "headers": headers,
        },
        {
            "method": "GET",
            "path": base,
            "query": {
                "actor_id": "alice",
                "path": "/preferences",
            },
            "headers": headers,
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
    assert client.get_store().name == "memory-stores/support-memory"


def test_injected_client_cannot_be_combined_with_auth_selectors() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        DatabricksManagedMemoryClient(
            "support-memory",
            workspace_host="https://workspace.example.com",
            workspace_client=cast("WorkspaceClient", _RecordingWorkspaceClient()),
        )


def test_missing_optional_sdk_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def import_without_sdk(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> object:
        if name == "databricks.sdk":
            raise ImportError("No module named 'databricks.sdk'")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_sdk)

    with pytest.raises(ImportError, match=r"pip install 'omnigent\[databricks\]'"):
        DatabricksManagedMemoryClient("support-memory")


def test_injected_client_does_not_require_optional_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def import_without_sdk(
        name: str,
        globals: Mapping[str, object] | None = None,
        locals: Mapping[str, object] | None = None,
        fromlist: Sequence[str] | None = (),
        level: int = 0,
    ) -> object:
        if name == "databricks.sdk":
            raise AssertionError("injected clients must not import databricks-sdk")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", import_without_sdk)
    workspace_client = _RecordingWorkspaceClient()

    client = DatabricksManagedMemoryClient(
        "support-memory",
        workspace_client=cast("WorkspaceClient", workspace_client),
    )

    assert client.get_store().name == "memory-stores/support-memory"


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
        client.update_entry("entry", cast("ManagedMemoryEntry", object()), update_mask=" ")


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
        client.get_effective_permission()
    assert raised.value is error


def test_non_object_response_fails_explicitly(
    client_and_api_client: tuple[DatabricksManagedMemoryClient, _RecordingApiClient],
) -> None:
    client, api_client = client_and_api_client
    api_client.response = []

    with pytest.raises(RuntimeError, match="non-object response"):
        client.get_effective_permission()


def test_raw_requests_allow_an_unset_workspace_id() -> None:
    workspace_client = _RecordingWorkspaceClient()
    workspace_client.config.workspace_id = None
    client = DatabricksManagedMemoryClient(
        "support-memory", workspace_client=cast("WorkspaceClient", workspace_client)
    )

    client.get_effective_permission()

    assert workspace_client.api_client.calls[0]["headers"] == {"Accept": "application/json"}
