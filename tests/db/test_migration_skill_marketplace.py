"""The skills marketplace tables: created, removed, repaired, and bounded on every write."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlInstalledSkill, SqlSkillPrincipal, SqlSkillSource
from omnigent.db.utils import _build_alembic_config, clear_engine_cache, get_or_create_engine

PREVIOUS = "d0a1f879a01d"
TABLES = {"skill_sources", "installed_skills", "skill_principals"}
# Index name -> its columns in order. A prefix or tie-breaker lost from a
# pagination index keeps the name and would pass a names-only check.
INDEXES: dict[str, dict[str, list[str]]] = {
    "skill_sources": {
        "ix_skill_sources_order": ["workspace_id", "created_at", "id"],
        "ux_skill_sources_identity": ["workspace_id", "url", "ref", "path"],
    },
    "installed_skills": {
        "ux_installed_skills_identity": ["workspace_id", "principal", "name"],
        "ix_installed_skills_principal_order": [
            "workspace_id",
            "principal",
            "installed_at",
            "id",
        ],
    },
    "skill_principals": {},
}
UNIQUE = {"ux_skill_sources_identity", "ux_installed_skills_identity"}
PRIMARY_KEYS = {
    "skill_sources": ["workspace_id", "id"],
    "installed_skills": ["workspace_id", "id"],
    "skill_principals": ["workspace_id", "principal"],
}


def _schema(connection: sa.Connection) -> tuple[set[str], dict[str, dict[str, list[str]]]]:
    inspector = sa.inspect(connection)
    tables = TABLES & set(inspector.get_table_names())
    indexes = {
        t: {ix["name"]: list(ix["column_names"]) for ix in inspector.get_indexes(t)}
        for t in tables
    }
    return tables, indexes


def _assert_at_head(connection: sa.Connection) -> None:
    tables, indexes = _schema(connection)
    assert tables == TABLES
    for table, expected in INDEXES.items():
        # Columns and their order, so a lost prefix or tie-breaker is caught.
        assert {name: indexes[table].get(name) for name in expected} == expected, indexes[table]
        assert (
            sa.inspect(connection).get_pk_constraint(table)["constrained_columns"]
            == PRIMARY_KEYS[table]
        ), table


def test_upgrade_creates_the_tables_and_downgrade_removes_them(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'skills-migration.db'}"
    engine = get_or_create_engine(uri)  # migrates to head
    config = _build_alembic_config(uri)
    try:
        _assert_lifecycle(engine, config)
    finally:
        engine.dispose()
        clear_engine_cache()


def _assert_lifecycle(engine: sa.Engine, config: object) -> None:
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        _assert_at_head(connection)
        inspector = sa.inspect(connection)
        for table in ("skill_sources", "installed_skills"):
            for index in inspector.get_indexes(table):
                assert index["unique"] == (index["name"] in UNIQUE), index
        columns = {c["name"]: c for c in inspector.get_columns("installed_skills")}
        assert {
            "principal",
            "owner_user_id",
            "bundle_bytes",
            "bundle_entries",
            "scope",
            "enabled",
            "user_invocable",
        } <= set(columns)
        assert columns["user_invocable"]["nullable"] is False
        assert columns["principal"]["nullable"] is False
        assert columns["owner_user_id"]["nullable"] is True
        sources = {c["name"]: c for c in inspector.get_columns("skill_sources")}
        assert sources["ref"]["nullable"] is False
        assert sources["path"]["nullable"] is False
        assert [c["name"] for c in inspector.get_columns("skill_principals")] == [
            "workspace_id",
            "principal",
            "installed_count",
            "enabled_bytes",
            "enabled_entries",
            "created_at",
        ]

        command.downgrade(config, PREVIOUS)
        assert _schema(connection)[0] == set()
        # Re-running the upgrade is safe.
        command.upgrade(config, "head")
        _assert_at_head(connection)

        # A run interrupted between a table and its index: the next run adds the
        # missing index, and the downgrade still removes everything.
        connection.execute(sa.text("DROP INDEX ix_skill_sources_order"))
        command.stamp(config, PREVIOUS)
        command.upgrade(config, "head")
        _assert_at_head(connection)
        command.downgrade(config, PREVIOUS)
        assert _schema(connection)[0] == set()

        # An index of the right name but the wrong uniqueness is refused, not kept.
        command.upgrade(config, "head")
        connection.execute(sa.text("DROP INDEX ux_skill_sources_identity"))
        connection.execute(
            sa.text("CREATE INDEX ux_skill_sources_identity ON skill_sources (workspace_id, url)")
        )
        command.stamp(config, PREVIOUS)
        with pytest.raises(RuntimeError, match=r"ux_skill_sources_identity.*unique=False"):
            command.upgrade(config, "head")
        connection.execute(sa.text("DROP INDEX ux_skill_sources_identity"))
        command.upgrade(config, "head")
        command.downgrade(config, PREVIOUS)

        # A pre-existing table with a different column set is refused, not stamped over.
        connection.execute(sa.text("CREATE TABLE skill_sources (workspace_id INTEGER, id BLOB)"))
        with pytest.raises(RuntimeError, match="skill_sources already exists"):
            command.upgrade(config, "head")


def test_a_bulk_write_cannot_slip_past_a_declared_length(db_uri: str) -> None:
    # A bulk insert skips the attribute validators, and SQLite enforces no
    # declared length, so the column type is what has to refuse the value.
    # 20,000 ASCII bytes would otherwise be stored, past the 16 KiB limit.
    engine = get_or_create_engine(db_uri)
    rows = [
        {
            "workspace_id": 0,
            "id": uuid.uuid4().hex,
            "url": "https://github.com/" + "a" * 20_000,
            "ref": "HEAD",
            "path": "",
            "name": "x",
            "created_by": None,
            "created_at": 0,
        }
    ]
    with Session(engine) as session:
        with pytest.raises(sa.exc.StatementError, match="longer than 255"):
            session.execute(sa.insert(SqlSkillSource), rows)
        session.rollback()
        assert session.scalar(sa.select(sa.func.count()).select_from(SqlSkillSource)) == 0


def test_declared_lengths_are_enforced_on_every_orm_write(db_uri: str) -> None:
    # SQLite ignores String(n); the models refuse an oversized value before it is written.
    with Session(get_or_create_engine(db_uri)) as session:
        with pytest.raises(ValueError, match="url is longer than 255"):
            SqlSkillSource(
                id=uuid.uuid4().hex,
                url="https://github.com/" + "a" * 20_000,
                ref="HEAD",
                path="",
                name="x",
                created_by=None,
                created_at=0,
            )
        with pytest.raises(ValueError, match="principal is longer than 133"):
            SqlSkillPrincipal(
                principal="user:" + "u" * 129,
                installed_count=0,
                enabled_bytes=0,
                enabled_entries=0,
                created_at=0,
            )
        row = SqlInstalledSkill(
            id=uuid.uuid4().hex,
            name="review",
            display_name=None,
            description="d" * 1024,
            source_url="https://github.com/acme/skills.git",
            ref=None,
            commit=None,
            path="skills/review",
            scope=1,
            principal="user:alice",
            owner_user_id="alice",
            bundle_location="skills/review.tar.gz",
            bundle_bytes=1,
            bundle_entries=1,
            enabled=True,
            user_invocable=True,
            installed_by="alice",
            installed_at=0,
        )
        session.add(row)
        session.flush()
        with pytest.raises(ValueError, match="description is longer than 1024"):
            row.description = "名" * 1025
        session.rollback()


def _source(url: str, ref: str, path: str = "") -> SqlSkillSource:
    return SqlSkillSource(
        id=uuid.uuid4().hex,
        url=url,
        ref=ref,
        path=path,
        name="Acme",
        created_by=None,
        created_at=0,
    )


def _installed(name: str, principal: str, *, scope: int = 2) -> SqlInstalledSkill:
    return SqlInstalledSkill(
        id=uuid.uuid4().hex,
        name=name,
        display_name=None,
        description="d",
        source_url="https://github.com/acme/skills.git",
        ref=None,
        commit=None,
        path=f"skills/{name}",
        scope=scope,
        principal=principal,
        owner_user_id=None if principal == "org" else principal.removeprefix("user:"),
        bundle_location=f"skills/{name}.tar.gz",
        bundle_bytes=1,
        bundle_entries=1,
        enabled=True,
        user_invocable=True,
        installed_by=None,
        installed_at=0,
    )


def test_identities_are_unique_on_the_engine(db_uri: str) -> None:
    # An exact duplicate of (url, ref, path) or (principal, name) is refused by the
    # index, while rows that differ only in case, ref, path or principal coexist: git refs,
    # repository paths and skill names are case-sensitive, so the identity
    # columns compare byte for byte on every engine (utf8mb4_bin on MySQL).
    engine = get_or_create_engine(db_uri)
    url = "https://github.com/acme/skills.git"
    with Session(engine) as session:
        session.add_all(
            [
                _source(url, "main"),
                _source(url, "Main"),
                _source(url, "HEAD"),
                _source(url, "main", "team-a"),
                _source(url, "main", "Team-A"),
                # The longest identity still fits MySQL's index key.
                _source("https://github.com/" + "a" * 236, "r" * 255, "p" * 200),
                _source("https://github.com/acme/Skills.git", "main"),
            ]
        )
        session.flush()
        session.add(_source(url, "main"))
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()

        session.add_all(
            [
                _installed("review", "org"),
                _installed("Review", "org"),
                _installed("review", "user:alice"),
                _installed("review", "user:Alice"),
            ]
        )
        session.flush()
        session.add(_installed("review", "org"))
        with pytest.raises(sa.exc.IntegrityError):
            session.flush()
        session.rollback()


def test_a_scope_outside_the_codec_is_refused_by_the_table(db_uri: str) -> None:
    with Session(get_or_create_engine(db_uri)) as session:
        session.add(_installed("review", "org", scope=3))
        # MySQL reports a check-constraint violation (error 3819) as OperationalError.
        with pytest.raises(
            (sa.exc.IntegrityError, sa.exc.OperationalError),
            match=r"ck_installed_skills_scope|CHECK",
        ):
            session.flush()
        session.rollback()
