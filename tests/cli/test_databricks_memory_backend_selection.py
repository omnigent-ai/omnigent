"""Server configuration and startup checks against the real Databricks SDK."""

from __future__ import annotations

import builtins

import click
import pytest
from click.testing import CliRunner

from omnigent.cli import _create_memory_store, cli
from omnigent.stores.memory_store.databricks_store import DatabricksManagedMemoryStore
from omnigent.stores.memory_store.sqlalchemy_store import SqlAlchemyMemoryStore
from tests.stores.managed_memory_api import (
    HOST,
    SERVICE_TOKEN,
    STORE_ID,
    ManagedMemoryAPI,
)
from tests.stores.managed_memory_api import (
    managed_memory_api as managed_memory_api,
)
from tests.stores.memory_store_conformance import ALICE

pytestmark = pytest.mark.databricks


@pytest.fixture()
def memory_config(managed_memory_api: ManagedMemoryAPI, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("DATABRICKS_TOKEN", SERVICE_TOKEN)
    return {
        "backend": "databricks",
        "databricks": {
            "workspace_host": HOST,
            "store_id": STORE_ID,
            "namespace": "support-oss",
            "auth_type": "pat",
        },
    }


def test_configuration_selects_the_backend_and_checks_store_access(
    db_uri: str, memory_config: dict, managed_memory_api: ManagedMemoryAPI
) -> None:
    store = _create_memory_store(None, db_uri, memory_config)

    assert isinstance(store, DatabricksManagedMemoryStore)
    assert [r["path"] for r in managed_memory_api.requests] == [
        f"/api/2.0/agents/memory-stores/{STORE_ID}",
        f"/api/2.0/agents/memory-stores/{STORE_ID}/permissions/self",
    ]
    store.write(ALICE, "/preferences", "Concise")
    assert store.get(ALICE, "/preferences").content == "Concise"


def test_flag_can_select_databricks_and_sqlalchemy_override_avoids_remote_calls(
    db_uri: str, memory_config: dict, managed_memory_api: ManagedMemoryAPI
) -> None:
    overridden = _create_memory_store("sqlalchemy", db_uri, memory_config)
    assert isinstance(overridden, SqlAlchemyMemoryStore)
    assert managed_memory_api.requests == []

    memory_config["backend"] = "sqlalchemy"
    assert isinstance(
        _create_memory_store("databricks", db_uri, memory_config), DatabricksManagedMemoryStore
    )


@pytest.mark.parametrize("permission", ["READ", "PERMISSION_LEVEL_UNSPECIFIED", None])
def test_startup_requires_write_permission(
    db_uri: str,
    memory_config: dict,
    managed_memory_api: ManagedMemoryAPI,
    permission: str | None,
) -> None:
    managed_memory_api.permission = permission

    with pytest.raises(click.ClickException, match="WRITE permission"):
        _create_memory_store(None, db_uri, memory_config)


@pytest.mark.parametrize(
    "failure", [(401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"), (404, "NOT_FOUND")]
)
def test_auth_or_store_failures_abort_startup_without_exposing_backend_details(
    db_uri: str,
    memory_config: dict,
    managed_memory_api: ManagedMemoryAPI,
    failure: tuple[int, str],
) -> None:
    managed_memory_api.failure = failure

    with pytest.raises(click.ClickException, match="Cannot connect") as error:
        _create_memory_store(None, db_uri, memory_config)

    assert "private-test-credential" not in str(error.value)


@pytest.mark.parametrize(
    "config",
    [
        None,
        "not-a-block",
        {},
        {"store_id": STORE_ID},
        {"store_id": STORE_ID, "namespace": " "},
        {"store_id": STORE_ID, "namespace": "x" * 129},
        {"store_id": "memory-stores/support-memory", "namespace": "support"},
        {"store_id": STORE_ID, "namespace": "support", "profile": 1},
        {"store_id": STORE_ID, "namespace": "support", "auth_type": ""},
        {"store_id": STORE_ID, "namespace": "support", "workspace_host": {}},
        {"store_id": STORE_ID, "namespace": "support", "client_secret": "private-value"},
    ],
)
def test_incomplete_or_unsupported_configuration_fails_before_any_remote_call(
    db_uri: str, managed_memory_api: ManagedMemoryAPI, config: object
) -> None:
    with pytest.raises(click.ClickException, match=r"memory\.databricks") as error:
        _create_memory_store("databricks", db_uri, {"databricks": config})

    assert "private-value" not in str(error.value)
    assert managed_memory_api.requests == []


def test_optional_sdk_is_only_required_for_the_selected_backend(
    db_uri: str, memory_config: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_import = builtins.__import__

    def without_sdk(name, *args, **kwargs):
        if name == "databricks.sdk":
            raise ImportError("optional SDK unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sdk)

    assert isinstance(
        _create_memory_store("sqlalchemy", db_uri, memory_config), SqlAlchemyMemoryStore
    )
    with pytest.raises(click.ClickException, match=r"omnigent\[databricks\]"):
        _create_memory_store(None, db_uri, memory_config)


def test_server_help_exposes_the_databricks_backend() -> None:
    result = CliRunner().invoke(cli, ["server", "--help"])

    assert result.exit_code == 0
    assert "sqlalchemy|hindsight|databricks" in result.output
