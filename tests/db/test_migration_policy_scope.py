"""Tests for the policies.scope migration (q1a2b3c4d5e6)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_scope_column_exists_and_is_not_nullable(db_engine: Engine) -> None:
    """policies.scope is a NOT NULL column after the migration."""
    columns = {c["name"]: c for c in sa.inspect(db_engine).get_columns("policies")}
    assert "scope" in columns, "policies.scope column must exist after migration"
    assert not columns["scope"]["nullable"], "policies.scope must be NOT NULL"


def test_ix_policies_name_cksum_index_exists(db_engine: Engine) -> None:
    """The default-name lookup index is present after the migration chain.

    At head the partial unique index (``ix_policies_default_name_cksum``) has
    been replaced by a plain non-unique ``name_cksum`` index (see
    z5a2b3c4d5e6); default-name uniqueness lives in the store.
    """
    indexes = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("policies")}
    assert "ix_policies_default_name_cksum" not in indexes
    assert "ix_policies_name_cksum" in indexes
    assert not indexes["ix_policies_name_cksum"]["unique"]


def test_backfill_sets_session_scope_for_session_policies(db_engine: Engine) -> None:
    """Rows with session_id set are back-filled with scope='session'."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                # kind/type run at head, where they are int codes
                # (1 = "default"/"python" per enum_codecs).
                "INSERT INTO conversations"
                " (id, created_at, updated_at, root_conversation_id, kind)"
                " VALUES ('conv_sc1', 1, 1, 'conv_sc1', 1)"
            )
        )
        conn.execute(
            sa.text(
                # scope runs at head, where it is an int code (2 = "session").
                # name_cksum is NOT NULL at head (x1a2b3c4d5e6) — sha256(name).
                "INSERT INTO policies"
                " (id, name, name_cksum, session_id, scope, created_at, type, handler, enabled)"
                " VALUES ('pol_sc1', 'sess_pol', :cksum, 'conv_sc1', 2, 1, 1, 'mod.f', 1)"
            ),
            {"cksum": hashlib.sha256(b"sess_pol").digest()},
        )
        scope = conn.execute(
            sa.text("SELECT scope FROM policies WHERE id = 'pol_sc1'")
        ).scalar_one()
    assert scope == 2


def test_backfill_sets_default_scope_for_default_policies(db_engine: Engine) -> None:
    """Rows with session_id NULL are back-filled with scope='default'."""
    with db_engine.begin() as conn:
        conn.execute(
            sa.text(
                # scope runs at head, where it is an int code (1 = "default").
                # name_cksum is NOT NULL at head (x1a2b3c4d5e6) — sha256(name).
                "INSERT INTO policies"
                " (id, name, name_cksum, session_id, scope, created_at, type, handler, enabled)"
                " VALUES ('pol_def1', 'def_pol', :cksum, NULL, 1, 1, 1, 'mod.f', 1)"
            ),
            {"cksum": hashlib.sha256(b"def_pol").digest()},
        )
        scope = conn.execute(
            sa.text("SELECT scope FROM policies WHERE id = 'pol_def1'")
        ).scalar_one()
    assert scope == 1


def test_scope_round_trip_via_store(db_engine: Engine) -> None:
    """scope is returned correctly by the policy store create methods."""
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    uri = str(db_engine.url)
    conv_store = SqlAlchemyConversationStore(uri)
    policy_store = SqlAlchemyPolicyStore(uri)

    session_id = conv_store.create_conversation().id

    session_pol = policy_store.create(
        policy_id="pol_rt_sess",
        session_id=session_id,
        name="session_policy",
        type="python",
        handler="mod.func",
    )
    default_pol = policy_store.create_default(
        policy_id="pol_rt_def",
        name="default_policy",
        type="python",
        handler="mod.func",
    )

    assert session_pol.scope == "session"
    assert default_pol.scope == "default"


def test_list_defaults_uses_scope_filter(db_engine: Engine) -> None:
    """list_defaults returns only scope='default' policies."""
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

    uri = str(db_engine.url)
    conv_store = SqlAlchemyConversationStore(uri)
    policy_store = SqlAlchemyPolicyStore(uri)

    session_id = conv_store.create_conversation().id
    policy_store.create(
        policy_id="pol_sess_x",
        session_id=session_id,
        name="sess_x",
        type="python",
        handler="mod.func",
    )
    policy_store.create_default(
        policy_id="pol_def_x",
        name="def_x",
        type="python",
        handler="mod.func",
    )

    defaults = policy_store.list_defaults()
    assert len(defaults) == 1
    assert defaults[0].id == "pol_def_x"
    assert defaults[0].scope == "default"


def test_downgrade_removes_scope_column(tmp_path: Path) -> None:
    """Downgrade drops policies.scope and ix_policies_default_name."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Start at head (includes the scope migration q1a2b3c4d5e6).
    assert "scope" in {c["name"] for c in sa.inspect(engine).get_columns("policies")}

    # Downgrade below the scope migration (through the later enums→SMALLINT
    # migration that now sits above it).
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "p1a2b3c4d5e6")

    columns = {c["name"] for c in sa.inspect(engine).get_columns("policies")}
    assert "scope" not in columns, "scope must be dropped by downgrade"

    index_names = {i["name"] for i in sa.inspect(engine).get_indexes("policies")}
    assert "ix_policies_default_name" not in index_names

    engine.dispose()
    clear_engine_cache()
