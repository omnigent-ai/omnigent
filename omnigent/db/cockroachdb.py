"""CockroachDB schema lifecycle: version gates, bootstrap, and migration.

CockroachDB starts from a baseline built directly from current ORM metadata
instead of replaying the PostgreSQL-oriented historical migration chain. This
module owns that lifecycle: server version and isolation validation, the
resumable bootstrap marker, model index verification, and post-baseline
Alembic upgrades. Shared engine, session, and retry helpers stay in
:mod:`omnigent.db.utils`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from packaging.version import InvalidVersion, Version
from sqlalchemy import Engine, Index, inspect, text

from omnigent.db.query_context import query_name_scope

_logger = logging.getLogger(__name__)

# The first revision that can be reached on CockroachDB without executing the
# PostgreSQL-oriented historical migration chain.
CRDB_BASELINE_REVISION = "ga1b2c3d4e5f"
CRDB_MINIMUM_VERSION = Version("23.2.28")
CRDB_TESTED_VERSIONS = frozenset(
    {Version("23.2.28"), Version("24.3.20"), Version("25.2.10"), Version("25.4.5")}
)
_CRDB_BOOTSTRAP_MARKER_TABLE = "omnigent_crdb_bootstrap"
_CRDB_BOOTSTRAP_MARKER_TOKEN = "omnigent-crdb-bootstrap-v1"


def _crdb_server_version(engine: Engine) -> Version:
    """Return and validate the CockroachDB server version."""
    with engine.connect() as connection:
        raw = str(connection.execute(text("SELECT version()")).scalar_one())
    return _parse_crdb_server_version(raw)


def _parse_crdb_server_version(raw: str) -> Version:
    """Parse and validate a CockroachDB server version string."""
    match = re.search(r"CockroachDB(?: \w+)? v(\d+\.\d+\.\d+)", raw)
    if match is None:
        raise RuntimeError(f"Could not determine the CockroachDB version from {raw!r}.")
    try:
        version = Version(match.group(1))
    except InvalidVersion as exc:
        raise RuntimeError(f"CockroachDB returned an invalid version string: {raw!r}.") from exc
    if version < CRDB_MINIMUM_VERSION:
        raise RuntimeError(
            f"CockroachDB {version} is unsupported. Omnigent requires "
            f"CockroachDB {CRDB_MINIMUM_VERSION} or newer."
        )
    if version not in CRDB_TESTED_VERSIONS:
        _logger.warning(
            "CockroachDB %s is newer than the minimum but outside Omnigent's "
            "release-tested matrix (%s).",
            version,
            ", ".join(str(item) for item in sorted(CRDB_TESTED_VERSIONS)),
        )
    return version


def _verify_crdb_read_committed(engine: Engine, version: Version) -> None:
    """Fail when CRDB silently substitutes SERIALIZABLE isolation."""
    with engine.connect() as connection:
        effective = str(
            connection.execute(text("SHOW transaction_isolation")).scalar_one()
        ).lower()
    if effective.replace("_", " ") == "read committed":
        return

    setting_hint = ""
    if version < Version("24.1"):
        setting_hint = (
            " Enable it with `SET CLUSTER SETTING "
            "sql.txn.read_committed_isolation.enabled = true;`, then restart Omnigent."
        )
    raise RuntimeError(
        f"CockroachDB {version} did not honor READ COMMITTED isolation "
        f"(effective isolation: {effective!r}). Omnigent requires READ COMMITTED."
        f"{setting_hint}"
    )


def _enable_crdb_ddl_autocommit(connection: Any, version: Version) -> None:
    """Enable weak-isolation DDL autocommit where CRDB provides it."""
    if version >= Version("24.1"):
        connection.execute(text("SET autocommit_before_ddl = true"))
        connection.commit()


def _prepare_crdb_schema_transaction(connection: Any, version: Version) -> None:
    """Begin a SERIALIZABLE transaction suitable for CRDB schema changes."""
    _enable_crdb_ddl_autocommit(connection, version)
    connection.execute(text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))


def _crdb_revision_is_supported(db_uri: str, current: str, head: str) -> bool:
    """Return whether *current* is on the supported CRDB migration segment."""
    if current == head:
        return True
    from alembic.script import ScriptDirectory

    # Imported lazily: utils imports this module at load time.
    from omnigent.db.utils import _build_alembic_config

    script = ScriptDirectory.from_config(_build_alembic_config(db_uri))
    revisions = script.iterate_revisions(head, CRDB_BASELINE_REVISION)
    return current in {revision.revision for revision in revisions} | {CRDB_BASELINE_REVISION}


def _start_or_resume_crdb_bootstrap(
    engine: Engine,
    version: Version,
    existing_tables: set[str],
    expected_tables: set[str],
    target_revision: str,
) -> None:
    """Create or validate the marker that makes bootstrap resumable."""
    marker_exists = _CRDB_BOOTSTRAP_MARKER_TABLE in existing_tables
    application_tables = existing_tables - {
        _CRDB_BOOTSTRAP_MARKER_TABLE,
        "alembic_version",
    }
    if not marker_exists:
        if existing_tables:
            raise RuntimeError(
                "CockroachDB contains tables but has no supported Omnigent schema revision. "
                "Use a new empty database; PostgreSQL migrations and partial CRDB migration "
                "attempts cannot be upgraded safely."
            )
        with engine.connect() as connection:
            _prepare_crdb_schema_transaction(connection, version)
            connection.execute(
                text(
                    f"CREATE TABLE {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                    "(token STRING PRIMARY KEY, target_revision STRING NOT NULL)"
                )
            )
            connection.commit()
            connection.execute(
                text(
                    f"INSERT INTO {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                    "(token, target_revision) VALUES (:token, :target_revision)"
                ),
                {
                    "token": _CRDB_BOOTSTRAP_MARKER_TOKEN,
                    "target_revision": target_revision,
                },
            )
            connection.commit()
        return

    unexpected = application_tables - expected_tables
    with engine.connect() as connection:
        markers = list(
            connection.execute(
                text(f"SELECT token, target_revision FROM {_CRDB_BOOTSTRAP_MARKER_TABLE}")
            )
        )
    expected_marker = (_CRDB_BOOTSTRAP_MARKER_TOKEN, target_revision)
    if markers == [expected_marker] and not unexpected:
        _logger.warning("Resuming an interrupted CockroachDB schema bootstrap.")
        return
    if not markers and not application_tables:
        # Recover the narrow interruption window between marker DDL and its
        # identifying row. No application table exists yet, so this is safe.
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO {_CRDB_BOOTSTRAP_MARKER_TABLE} "
                    "(token, target_revision) VALUES (:token, :target_revision)"
                ),
                {
                    "token": _CRDB_BOOTSTRAP_MARKER_TOKEN,
                    "target_revision": target_revision,
                },
            )
        return
    raise RuntimeError(
        "CockroachDB has an invalid Omnigent bootstrap marker or unexpected tables. "
        "Use a new empty database rather than stamping an unknown partial schema."
    )


def _crdb_model_indexes() -> tuple[Index, ...]:
    """Return every index declared by the current application models."""
    from omnigent.db.db_models import ConversationBase, OmnigentBase

    entries = (
        (table.name, index)
        for metadata in (OmnigentBase.metadata, ConversationBase.metadata)
        for table in metadata.tables.values()
        for index in table.indexes
    )
    return tuple(
        index for _, index in sorted(entries, key=lambda entry: (entry[0], entry[1].name or ""))
    )


def _repair_and_verify_crdb_model_indexes(engine: Engine, version: Version) -> None:
    """Create missing model indexes and verify them before bootstrap stamping."""
    indexes = _crdb_model_indexes()
    attached_indexes: list[tuple[Index, str]] = []
    for index in indexes:
        table = index.table
        if table is None:
            raise RuntimeError(
                f"CockroachDB model index {index.name!r} is not attached to a table."
            )
        attached_indexes.append((index, table.name))
    inspector = inspect(engine)
    found_by_table = {
        table_name: {
            str(index["name"])
            for index in inspector.get_indexes(table_name)
            if index.get("name") is not None
        }
        for table_name in {table_name for _, table_name in attached_indexes}
    }
    missing = [
        index
        for index, table_name in attached_indexes
        if index.name is not None and index.name not in found_by_table[table_name]
    ]
    for index in missing:
        with engine.connect() as connection:
            _prepare_crdb_schema_transaction(connection, version)
            index.create(bind=connection, checkfirst=True)
            connection.commit()

    verified = inspect(engine)
    still_missing = [
        f"{table_name}.{index.name}"
        for index, table_name in attached_indexes
        if index.name is not None and not verified.has_index(table_name, index.name)
    ]
    if still_missing:
        raise RuntimeError(
            "CockroachDB schema bootstrap did not create expected indexes: "
            + ", ".join(still_missing)
        )


def _finish_crdb_bootstrap(engine: Engine, version: Version) -> None:
    """Remove the bootstrap marker after the Alembic revision is durable."""
    if _CRDB_BOOTSTRAP_MARKER_TABLE not in inspect(engine).get_table_names():
        return
    with engine.connect() as connection:
        _prepare_crdb_schema_transaction(connection, version)
        connection.execute(text(f"DROP TABLE {_CRDB_BOOTSTRAP_MARKER_TABLE}"))
        connection.commit()


def _initialize_or_verify_crdb_schema(engine: Engine, db_uri: str) -> None:
    """Bootstrap an empty CRDB database or upgrade a supported CRDB schema."""
    from alembic import command

    from omnigent.db.db_models import ConversationBase, OmnigentBase

    # Imported lazily: utils imports this module at load time.
    from omnigent.db.utils import (
        _build_alembic_config,
        _get_current_db_revision,
        _get_head_db_revision,
        _run_migrations,
        _verify_db_revision_is_supported,
    )

    version = _crdb_server_version(engine)
    _verify_crdb_read_committed(engine, version)
    head = _get_head_db_revision(db_uri)
    current = _get_current_db_revision(engine)
    tables = set(inspect(engine).get_table_names())
    expected = set(OmnigentBase.metadata.tables) | set(ConversationBase.metadata.tables)

    if current is None:
        _start_or_resume_crdb_bootstrap(engine, version, tables, expected, head)
        with query_name_scope("omnigent.database.bootstrap_cockroachdb"):
            with engine.connect() as connection:
                _prepare_crdb_schema_transaction(connection, version)
                OmnigentBase.metadata.create_all(bind=connection)
                connection.commit()
                _prepare_crdb_schema_transaction(connection, version)
                ConversationBase.metadata.create_all(bind=connection)
                connection.commit()
            missing = expected - set(inspect(engine).get_table_names())
            if missing:
                raise RuntimeError(
                    "CockroachDB schema bootstrap did not create expected tables: "
                    + ", ".join(sorted(missing))
                )
            _repair_and_verify_crdb_model_indexes(engine, version)
            config = _build_alembic_config(db_uri)
            with engine.connect() as connection:
                _prepare_crdb_schema_transaction(connection, version)
                config.attributes["connection"] = connection
                command.stamp(config, "head")
                connection.commit()
            if _get_current_db_revision(engine) != head:
                raise RuntimeError(
                    "CockroachDB schema bootstrap did not stamp the expected Alembic head "
                    f"{head!r}. The bootstrap marker was retained for a safe retry."
                )
            _finish_crdb_bootstrap(engine, version)
        return

    _verify_db_revision_is_supported(db_uri, current, head)
    if not _crdb_revision_is_supported(db_uri, current, head):
        raise RuntimeError(
            f"CockroachDB schema revision {current!r} predates Omnigent's CRDB "
            f"baseline {CRDB_BASELINE_REVISION!r}. Use a new empty database."
        )
    if current != head:
        _logger.warning(
            "CockroachDB schema is out of date (found revision %r, expected %r); "
            "attempting automatic migration.",
            current,
            head,
        )
        try:
            _run_migrations(engine, db_uri)
        except Exception as exc:
            raise RuntimeError(
                "CockroachDB schema migration failed "
                f"(found revision {current!r}, expected {head!r}). "
                "Take a backup, then run\n\n"
                f"    omnigent debug db-upgrade {db_uri!r}\n\n"
                "to inspect or retry the migration manually."
            ) from exc
        migrated = _get_current_db_revision(engine)
        if migrated != head:
            raise RuntimeError(
                "CockroachDB schema migration did not reach head "
                f"(started at {current!r}, now at {migrated!r}, expected {head!r}). "
                "Take a backup, then run\n\n"
                f"    omnigent debug db-upgrade {db_uri!r}\n\n"
                "to inspect or retry the migration manually."
            )
    _finish_crdb_bootstrap(engine, version)
