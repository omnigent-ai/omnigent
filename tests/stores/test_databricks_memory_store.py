"""MemoryStore conformance through the real Databricks SDK and a REST stand-in."""

from __future__ import annotations

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import MemoryStore
from omnigent.stores.memory_store.databricks_store import DatabricksManagedMemoryStore
from tests.stores.managed_memory_api import (
    SERVICE_TOKEN,
    ManagedMemoryAPI,
)
from tests.stores.managed_memory_api import (
    managed_memory_api as managed_memory_api,
)
from tests.stores.memory_store_conformance import ALICE, BOB, SearchableMemoryStoreConformance

pytestmark = pytest.mark.databricks


@pytest.fixture()
def store(managed_memory_api: ManagedMemoryAPI) -> DatabricksManagedMemoryStore:
    return DatabricksManagedMemoryStore(managed_memory_api.client(), namespace="support-oss")


class TestDatabricksMemoryStore(SearchableMemoryStoreConformance):
    """The backend-independent contract, including actor and workspace isolation."""


def test_namespace_isolates_deployments_and_survives_reconstruction(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore
) -> None:
    store.write(ALICE, "/p", "first deployment")
    same = DatabricksManagedMemoryStore(managed_memory_api.client(), namespace="support-oss")
    other = DatabricksManagedMemoryStore(managed_memory_api.client(), namespace="other-oss")

    assert same.get(ALICE, "/p").content == "first deployment"
    assert other.get(ALICE, "/p") is None
    other.write(ALICE, "/p", "second deployment")

    assert store.get(ALICE, "/p").content == "first deployment"
    assert len(managed_memory_api.entries) == 2
    assert len({entry["actor_id"] for entry in managed_memory_api.entries.values()}) == 2
    assert all(
        request["authorization"] == f"Bearer {SERVICE_TOKEN}"
        for request in managed_memory_api.requests
    )


def test_writes_use_stable_ids_and_explicit_agent_source(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore
) -> None:
    store.write(ALICE, "/p", "first", "summary")
    first_id = next(iter(managed_memory_api.entries))
    store.write(ALICE, "/p", "second")

    assert list(managed_memory_api.entries) == [first_id]
    entry = managed_memory_api.entries[first_id]
    assert entry["content"] == "second"
    assert entry["description"] is None
    assert entry["source_type"] == "MANAGED_MEMORY_ENTRY_SOURCE_TYPE_AGENT"
    assert "session_id" not in entry


def test_a_concurrent_first_write_is_replaced(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore
) -> None:
    managed_memory_api.race_create = True

    saved = store.write(ALICE, "/p", "our content")

    assert saved.content == "our content"
    assert len(managed_memory_api.entries) == 1
    assert [r["method"] for r in managed_memory_api.requests].count("PATCH") == 1


def test_a_concurrent_delete_returns_false(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore
) -> None:
    store.write(ALICE, "/p", "x")
    managed_memory_api.race_delete = True

    assert store.delete(ALICE, "/p") is False
    assert store.get(ALICE, "/p") is None


def test_short_and_empty_filtered_pages_preserve_the_server_cursor(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore
) -> None:
    managed_memory_api.list_page_cap = 1
    for path in ("/projects-old/a", "/projects", "/projects/api", "/projects-old/b"):
        store.write(ALICE, path, "x")

    first = store.list(ALICE, "/projects", page_size=10)
    assert first.entries == []
    assert first.next_page_token
    paths = []
    token = first.next_page_token
    while token:
        page = store.list(ALICE, "/projects", page_token=token, page_size=10)
        paths.extend(entry.path for entry in page.entries)
        token = page.next_page_token

    assert paths == ["/projects", "/projects/api"]


@pytest.mark.parametrize("scope", ["actor", "workspace", "namespace", "prefix"])
def test_cursor_cannot_switch_partitions(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore, scope: str
) -> None:
    for path in ("/p/a", "/p/b"):
        store.write(ALICE, path, "x")
    token = store.list(ALICE, page_size=1).next_page_token
    assert token

    with pytest.raises(OmnigentError) as error:
        if scope == "workspace":
            with workspace_scope(7):
                store.list(ALICE, page_token=token)
        elif scope == "namespace":
            other = DatabricksManagedMemoryStore(managed_memory_api.client(), namespace="other")
            other.list(ALICE, page_token=token)
        else:
            store.list(
                BOB if scope == "actor" else ALICE,
                path_prefix="/p" if scope == "prefix" else None,
                page_token=token,
            )
    assert error.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("operation", ["get", "list", "search", "write", "delete"])
@pytest.mark.parametrize("field", ["actor_id", "path", "invalid_path", "name", "session_id"])
def test_unexpected_entry_identity_is_rejected_before_mutation(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore, operation: str, field: str
) -> None:
    store.write(ALICE, "/p", "private content")
    original = dict(next(iter(managed_memory_api.entries.values())))
    tampered = {**original, field: "unexpected" if field != "path" else "/other"}
    if field == "invalid_path":
        tampered["path"] = "/bad/../path"
    managed_memory_api.response_override = (
        {"managed_memory_entries": [tampered]}
        if operation == "list"
        else {"results": [{"managed_memory_entry": tampered, "score": 1.0}]}
        if operation == "search"
        else tampered
    )
    requests_before = len(managed_memory_api.requests)

    with pytest.raises(OmnigentError) as error:
        _operation(store, operation)

    assert error.value.code == ErrorCode.INTERNAL_ERROR
    assert all(
        request["method"] == "GET"
        or (operation == "search" and request["path"].endswith("/entries:search"))
        for request in managed_memory_api.requests[requests_before:]
    )
    assert next(iter(managed_memory_api.entries.values())) == original


@pytest.mark.parametrize("operation", ["get", "list", "search", "write", "delete"])
@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (401, "UNAUTHENTICATED", ErrorCode.INTERNAL_ERROR),
        (403, "PERMISSION_DENIED", ErrorCode.UPSTREAM_PERMISSION_DENIED),
    ],
)
def test_backend_auth_failures_are_explicit_and_do_not_expose_credentials(
    managed_memory_api: ManagedMemoryAPI,
    store: MemoryStore,
    operation: str,
    status: int,
    code: str,
    expected: ErrorCode,
) -> None:
    managed_memory_api.failure = status, code

    with pytest.raises(OmnigentError) as error:
        _operation(store, operation)

    assert error.value.code == expected
    assert "private-test-credential" not in str(error.value)


@pytest.mark.parametrize("operation", ["get", "list", "search", "write", "delete"])
def test_a_missing_store_is_not_reported_as_missing_memory(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore, operation: str
) -> None:
    managed_memory_api.missing_store = True

    with pytest.raises(OmnigentError) as error:
        _operation(store, operation)

    assert error.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize("namespace", ["", " ", "x" * 129])
def test_namespace_is_required(managed_memory_api: ManagedMemoryAPI, namespace: str) -> None:
    with pytest.raises(ValueError, match="namespace"):
        DatabricksManagedMemoryStore(managed_memory_api.client(), namespace=namespace)


@pytest.mark.parametrize(
    "response",
    [
        {"managed_memory_entries": None},
        {"managed_memory_entries": ["invalid"]},
        {"next_page_token": 123},
        {"next_page_token": False},
    ],
)
def test_malformed_pages_fail_explicitly(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore, response: dict
) -> None:
    managed_memory_api.response_override = response

    with pytest.raises(OmnigentError) as error:
        store.list(ALICE)

    assert error.value.code == ErrorCode.INTERNAL_ERROR


@pytest.mark.parametrize("score", [None, float("nan"), float("inf"), True])
def test_incomplete_or_invalid_search_scores_fail_explicitly(
    managed_memory_api: ManagedMemoryAPI, store: MemoryStore, score: object
) -> None:
    store.write(ALICE, "/p", "content")
    entry = next(iter(managed_memory_api.entries.values()))
    managed_memory_api.response_override = {
        "results": [{"managed_memory_entry": entry, "score": score}]
    }

    with pytest.raises(OmnigentError) as error:
        store.search(ALICE, "content")

    assert error.value.code == ErrorCode.INTERNAL_ERROR


def _operation(store: MemoryStore, operation: str) -> object:
    if operation == "list":
        return store.list(ALICE)
    if operation == "search":
        return store.search(ALICE, "private")
    if operation == "write":
        return store.write(ALICE, "/p", "replacement")
    if operation == "delete":
        return store.delete(ALICE, "/p")
    return store.get(ALICE, "/p")
