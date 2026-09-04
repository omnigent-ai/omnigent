"""Focused tests for CockroachDB engine and schema safeguards."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any
from unittest.mock import MagicMock

import pytest
from packaging.version import Version
from sqlalchemy.exc import NoSuchModuleError

from omnigent.db import utils


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CockroachDB v23.2.28 (x86_64)", Version("23.2.28")),
        ("CockroachDB CCL v24.3.20 (x86_64)", Version("24.3.20")),
        ("CockroachDB OSS v25.2.10 (x86_64)", Version("25.2.10")),
        ("CockroachDB Enterprise v25.4.5 (x86_64)", Version("25.4.5")),
    ],
)
def test_parse_crdb_server_version_accepts_distribution_labels(
    raw: str,
    expected: Version,
) -> None:
    assert utils._parse_crdb_server_version(raw) == expected


def test_parse_crdb_server_version_rejects_versions_below_minimum() -> None:
    with pytest.raises(RuntimeError, match=r"requires CockroachDB 23\.2\.28 or newer"):
        utils._parse_crdb_server_version("CockroachDB OSS v23.2.27 (x86_64)")


def test_parse_crdb_server_version_warns_outside_tested_matrix(
    caplog: pytest.LogCaptureFixture,
) -> None:
    version = utils._parse_crdb_server_version("CockroachDB CCL v26.1.0 (x86_64)")

    assert version == Version("26.1.0")
    assert "outside Omnigent's release-tested matrix" in caplog.text


def test_crdb_engine_reports_missing_optional_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_dialect(_uri: str, **_kwargs: Any) -> None:
        raise NoSuchModuleError("cockroachdb.psycopg")

    monkeypatch.setattr(utils, "create_engine", missing_dialect)

    with pytest.raises(RuntimeError, match=r"omnigent\[cockroachdb\]"):
        utils._create_engine("cockroachdb://root@localhost/defaultdb")


def test_verify_crdb_read_committed_rejects_silent_v23_fallback() -> None:
    result = MagicMock()
    result.scalar_one.return_value = "serializable"
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match=r"sql\.txn\.read_committed_isolation\.enabled"):
        utils._verify_crdb_read_committed(engine, Version("23.2.28"))


def test_verify_crdb_read_committed_accepts_effective_level() -> None:
    result = MagicMock()
    result.scalar_one.return_value = "read committed"
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    utils._verify_crdb_read_committed(engine, Version("23.2.28"))


def test_crdb_pool_overrides_treat_blank_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", " ")
    monkeypatch.setenv("OMNIGENT_DB_MAX_OVERFLOW", "")
    monkeypatch.setenv("OMNIGENT_DB_POOL_TIMEOUT", "\t")
    monkeypatch.setattr(
        utils,
        "create_engine",
        lambda _uri, **kwargs: captured.update(kwargs) or MagicMock(),
    )

    utils._create_engine("cockroachdb://root@localhost/defaultdb")

    assert captured["pool_size"] == 200
    assert captured["max_overflow"] == 20
    assert captured["pool_timeout"] == 10.0


def test_crdb_pool_overrides_accept_sqlalchemy_ranges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OMNIGENT_DB_POOL_SIZE", "0")
    monkeypatch.setenv("OMNIGENT_DB_MAX_OVERFLOW", "-1")
    monkeypatch.setenv("OMNIGENT_DB_POOL_TIMEOUT", "0.25")
    monkeypatch.setattr(
        utils,
        "create_engine",
        lambda _uri, **kwargs: captured.update(kwargs) or MagicMock(),
    )

    utils._create_engine("cockroachdb://root@localhost/defaultdb")

    assert captured["pool_size"] == 0
    assert captured["max_overflow"] == -1
    assert captured["pool_timeout"] == 0.25


def test_prepare_crdb_schema_transaction_uses_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = MagicMock()
    enabled: list[Version] = []
    monkeypatch.setattr(
        utils,
        "_enable_crdb_ddl_autocommit",
        lambda _connection, version: enabled.append(version),
    )

    utils._prepare_crdb_schema_transaction(connection, Version("24.3.20"))

    assert enabled == [Version("24.3.20")]
    statement = str(connection.execute.call_args.args[0])
    assert statement == "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"


def test_bootstrap_rejects_unknown_unversioned_tables() -> None:
    engine = MagicMock()

    with pytest.raises(RuntimeError, match="Use a new empty database"):
        utils._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {"agents"},
            {"agents", "conversations"},
            "head",
        )

    engine.connect.assert_not_called()


def test_bootstrap_resumes_only_with_valid_marker() -> None:
    result = MagicMock()
    result.__iter__.return_value = iter([(utils._CRDB_BOOTSTRAP_MARKER_TOKEN, "head")])
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    utils._start_or_resume_crdb_bootstrap(
        engine,
        Version("25.2.10"),
        {utils._CRDB_BOOTSTRAP_MARKER_TABLE, "alembic_version", "agents"},
        {"agents", "conversations"},
        "head",
    )


def test_bootstrap_rejects_invalid_marker() -> None:
    result = MagicMock()
    result.__iter__.return_value = iter([("not-an-omnigent-marker", "head")])
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match="invalid Omnigent bootstrap marker"):
        utils._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {utils._CRDB_BOOTSTRAP_MARKER_TABLE, "agents"},
            {"agents", "conversations"},
            "head",
        )


def test_bootstrap_rejects_marker_from_different_schema_head() -> None:
    result = MagicMock()
    result.__iter__.return_value = iter([(utils._CRDB_BOOTSTRAP_MARKER_TOKEN, "older-head")])
    connection = MagicMock()
    connection.execute.return_value = result
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)

    with pytest.raises(RuntimeError, match="invalid Omnigent bootstrap marker"):
        utils._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {utils._CRDB_BOOTSTRAP_MARKER_TABLE, "agents"},
            {"agents", "conversations"},
            "current-head",
        )


def test_bootstrap_rejects_empty_alembic_table_without_marker() -> None:
    engine = MagicMock()

    with pytest.raises(RuntimeError, match="Use a new empty database"):
        utils._start_or_resume_crdb_bootstrap(
            engine,
            Version("25.2.10"),
            {"alembic_version"},
            {"agents", "conversations"},
            "head",
        )


def test_bootstrap_repairs_and_verifies_missing_model_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Table:
        def __init__(self, name: str) -> None:
            self.name = name

    class Index:
        def __init__(self, name: str, table: str, *, created: bool) -> None:
            self.name = name
            self.table = Table(table)
            self.created = created

        def create(self, *, bind: object, checkfirst: bool) -> None:
            assert bind is connection
            assert checkfirst is True
            self.created = True

    existing = Index("ix_agents_created_at", "agents", created=True)
    missing = Index("ix_agents_name", "agents", created=False)

    class Inspector:
        def get_indexes(self, table_name: str) -> list[dict[str, str]]:
            assert table_name == "agents"
            return [{"name": existing.name}]

        def has_index(self, table_name: str, index_name: str) -> bool:
            assert table_name == "agents"
            return index_name == existing.name or missing.created

    connection = MagicMock()
    engine = MagicMock()
    engine.connect.return_value = nullcontext(connection)
    prepare = MagicMock()
    monkeypatch.setattr(utils, "_crdb_model_indexes", lambda: (existing, missing))
    monkeypatch.setattr(utils, "inspect", lambda _engine: Inspector())
    monkeypatch.setattr(utils, "_prepare_crdb_schema_transaction", prepare)

    utils._repair_and_verify_crdb_model_indexes(engine, Version("25.2.10"))

    assert missing.created is True
    prepare.assert_called_once_with(connection, Version("25.2.10"))
    connection.commit.assert_called_once_with()
