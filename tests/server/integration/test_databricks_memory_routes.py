"""Existing authenticated memory journeys against the configured Databricks backend."""

from __future__ import annotations

import httpx
import pytest

from omnigent.cli import _create_memory_store
from omnigent.errors import ErrorCode
from omnigent.stores.memory_store import MemoryStore
from tests.server.integration import test_memory_routes as journeys
from tests.stores.managed_memory_api import (
    HOST,
    SERVICE_TOKEN,
    STORE_ID,
    ManagedMemoryAPI,
)
from tests.stores.managed_memory_api import (
    managed_memory_api as managed_memory_api,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.databricks]

ALICE = journeys.ALICE
BOB = journeys.BOB
auth_app = journeys.auth_app
memory_backend = journeys.memory_backend
with_permissions = journeys.with_permissions
test_owner_round_trip = journeys.test_owner_writes_reads_lists_and_deletes
test_user_isolation = journeys.test_users_cannot_see_each_others_memory
test_actor_is_server_derived = journeys.test_the_actor_cannot_be_supplied_by_the_request
test_invalid_paths = journeys.test_invalid_paths_return_400
test_search_isolation = journeys.test_a_backend_that_can_search_serves_only_the_callers_hits
test_capability = journeys.test_info_advertises_memory
test_feature_off = journeys.test_by_default_the_routes_and_the_capability_are_off
test_tool_round_trip = journeys.test_memory_tools_round_trip_through_the_real_server
test_tool_pagination = journeys.test_memory_list_pages_through_the_real_server
test_shared_tools = (
    journeys.test_memory_tools_are_refused_once_the_session_is_shared_with_an_editor
)
test_runner_binding = journeys.test_tool_call_without_the_bound_runner_token_is_refused
test_session_ownership = journeys.test_tool_call_naming_another_users_session_is_refused
test_permission_store = journeys.test_without_a_permission_store_tool_calls_are_refused
test_search_capability = journeys.test_bundle_download_offers_search_for_a_backend_that_can_search


@pytest.fixture()
def memory_store_override(
    db_uri: str, managed_memory_api: ManagedMemoryAPI, monkeypatch: pytest.MonkeyPatch
) -> MemoryStore:
    monkeypatch.setenv("DATABRICKS_TOKEN", SERVICE_TOKEN)
    return _create_memory_store(
        None,
        db_uri,
        {
            "backend": "databricks",
            "databricks": {
                "workspace_host": HOST,
                "store_id": STORE_ID,
                "namespace": "support-oss",
                "auth_type": "pat",
            },
        },
    )


async def test_each_users_partition_uses_the_same_server_credential(
    auth_client: httpx.AsyncClient, managed_memory_api: ManagedMemoryAPI
) -> None:
    for headers in (ALICE, BOB):
        result = await auth_client.put(
            "/v1/memory/entry",
            json={"path": "/preferences", "content": headers["X-Forwarded-Email"]},
            headers=headers,
        )
        assert result.status_code == 200, result.text
    alice = await auth_client.get(
        "/v1/memory/entry", params={"path": "/preferences"}, headers=ALICE
    )
    bob = await auth_client.get("/v1/memory/entry", params={"path": "/preferences"}, headers=BOB)

    assert alice.json()["content"] == "alice"
    assert bob.json()["content"] == "bob"
    assert len({entry["actor_id"] for entry in managed_memory_api.entries.values()}) == 2
    assert all(
        request["authorization"] == f"Bearer {SERVICE_TOKEN}"
        for request in managed_memory_api.requests
    )


async def test_backend_permission_loss_is_not_an_empty_list(
    auth_client: httpx.AsyncClient, managed_memory_api: ManagedMemoryAPI
) -> None:
    managed_memory_api.failure = 403, "PERMISSION_DENIED"

    response = await auth_client.get("/v1/memory/entries", headers=ALICE)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == ErrorCode.UPSTREAM_PERMISSION_DENIED
    assert "private-test-credential" not in response.text


async def test_long_backend_cursor_round_trips_through_the_routes(
    auth_client: httpx.AsyncClient,
) -> None:
    prefix = "/" + "p" * 450
    for suffix in ("/a", "/b"):
        written = await auth_client.put(
            "/v1/memory/entry",
            json={"path": prefix + suffix, "content": "x"},
            headers=ALICE,
        )
        assert written.status_code == 200, written.text
    first = await auth_client.get(
        "/v1/memory/entries", params={"prefix": prefix, "page_size": 1}, headers=ALICE
    )
    assert first.status_code == 200, first.text
    token = first.json()["next_page_token"]
    assert len(token) > 512

    second = await auth_client.get(
        "/v1/memory/entries",
        params={"prefix": prefix, "page_size": 1, "page_token": token},
        headers=ALICE,
    )

    assert second.status_code == 200, second.text
    assert second.json()["entries"][0]["path"] == prefix + "/b"
    assert second.json()["next_page_token"] is None

    oversized = await auth_client.get(
        "/v1/memory/entries", params={"page_token": "x" * 8193}, headers=ALICE
    )
    assert oversized.status_code == 422
