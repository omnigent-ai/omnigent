import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_mobile_push_migration_deployment_contract():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config("omnigent/db/alembic.ini"))
    assert "mp1b2c3d4e5f" in {
        ancestor.revision for ancestor in script.iterate_revisions("heads", "base")
    }
    revision = script.get_revision("mp1b2c3d4e5f")
    assert revision is not None
    assert revision.down_revision == "mm1a2b3c4d5e"
    assert script.get_revision(revision.down_revision) is not None
    for required in (
        "automatically migrates at startup",
        "deploy schema before feature code",
        "inert for older code",
        "old replicas refuse a newer schema",
        "flag-off roll-forward",
        "stop new replicas before downgrading",
        "OMNIGENT_DB_URL=… alembic -c omnigent/db/alembic.ini downgrade mm1a2b3c4d5e",
    ):
        assert required in revision.module.__doc__


def test_mobile_push_migration_up_down_and_workspace_keys(db_uri):
    engine = get_or_create_engine(db_uri)
    config = _build_alembic_config(db_uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "mm1a2b3c4d5e")
        assert "mobile_push_devices" not in sa.inspect(connection).get_table_names()
        command.upgrade(config, "head")
        inspector = sa.inspect(connection)
        assert inspector.get_pk_constraint("mobile_push_devices")["constrained_columns"] == [
            "workspace_id",
            "installation_id",
        ]
        assert inspector.get_pk_constraint("mobile_push_outbox")["constrained_columns"] == [
            "workspace_id",
            "id",
        ]
        assert {
            item["name"] for item in inspector.get_unique_constraints("mobile_push_devices")
        } == {"uq_mobile_push_device_token"}
        assert {
            item["name"] for item in inspector.get_unique_constraints("mobile_push_outbox")
        } == {"uq_mobile_push_outbox_intent"}
        assert "preview" not in {
            item["name"] for item in inspector.get_columns("mobile_push_outbox")
        }
        assert {
            tuple(index["column_names"]) for index in inspector.get_indexes("mobile_push_outbox")
        } == {
            ("workspace_id", "delivered", "not_before", "lease_until", "id"),
            ("expires_at", "workspace_id", "id"),
            ("workspace_id", "installation_id", "id"),
            ("workspace_id", "user_id", "id"),
            ("delivered", "workspace_id", "id"),
        }
        assert {
            tuple(index["column_names"]) for index in inspector.get_indexes("mobile_push_devices")
        } == {
            ("expires_at", "workspace_id", "installation_id"),
            ("workspace_id", "user_id", "expires_at", "installation_id"),
        }
        command.downgrade(config, "mm1a2b3c4d5e")
        assert "mobile_push_outbox" not in sa.inspect(connection).get_table_names()
        command.upgrade(config, "head")


@pytest.mark.parametrize(
    "table,column,type_class,length",
    [
        ("mobile_push_devices", "fcm_token", sa.String, 1024),
        ("mobile_push_devices", "platform", sa.SmallInteger, None),
        ("mobile_push_outbox", "kind", sa.SmallInteger, None),
        ("mobile_push_devices", "generation", sa.LargeBinary, 16),
        ("mobile_push_outbox", "id", sa.LargeBinary, 16),
        ("mobile_push_outbox", "device_generation", sa.LargeBinary, 16),
        ("mobile_push_outbox", "lease", sa.LargeBinary, 16),
    ],
)
def test_mobile_push_column_types(db_uri, table, column, type_class, length):
    from omnigent.db.db_models import OmnigentBase, Uuid16

    inspector = sa.inspect(get_or_create_engine(db_uri))
    stored = next(item for item in inspector.get_columns(table) if item["name"] == column)
    assert isinstance(stored["type"], type_class)
    model_type = OmnigentBase.metadata.tables[table].c[column].type
    if type_class is sa.LargeBinary:
        assert isinstance(model_type, Uuid16)
        assert model_type.impl.length == length
    else:
        assert isinstance(model_type, type_class)
        if length is not None:
            assert stored["type"].length == model_type.length == length


@pytest.mark.parametrize("table", ["mobile_push_devices", "mobile_push_outbox"])
def test_mobile_push_no_database_defaults(db_uri, table):
    from omnigent.db.db_models import OmnigentBase

    assert all(
        item["default"] is None
        for item in sa.inspect(get_or_create_engine(db_uri)).get_columns(table)
    )
    assert all(
        column.server_default is None for column in OmnigentBase.metadata.tables[table].columns
    )


@pytest.mark.parametrize(
    "table,column,codes",
    [("mobile_push_devices", "platform", "1, 2"), ("mobile_push_outbox", "kind", "1, 2, 3")],
)
def test_mobile_push_integer_checks(db_uri, table, column, codes):
    from omnigent.db.db_models import OmnigentBase

    expected = f"{column} IN ({codes})"
    inspector = sa.inspect(get_or_create_engine(db_uri))
    assert expected in {item["sqltext"] for item in inspector.get_check_constraints(table)}
    assert expected in {
        str(constraint.sqltext)
        for constraint in OmnigentBase.metadata.tables[table].constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


@pytest.mark.parametrize("table", ["mobile_push_devices", "mobile_push_outbox"])
def test_mobile_push_indexes_include_primary_keys(db_uri, table):
    from omnigent.db.db_models import OmnigentBase

    model = OmnigentBase.metadata.tables[table]
    stored = {
        item["name"]: tuple(item["column_names"])
        for item in sa.inspect(get_or_create_engine(db_uri)).get_indexes(table)
    }
    assert stored == {
        index.name: tuple(column.name for column in index.columns) for index in model.indexes
    }
    primary_key = tuple(column.name for column in model.primary_key)
    for columns in stored.values():
        missing = tuple(
            column for column in primary_key if column not in columns[: -len(primary_key)]
        )
        assert columns[-len(missing) :] == missing
    assert not any(
        other != name and columns == candidate[: len(columns)]
        for name, columns in stored.items()
        for other, candidate in stored.items()
    )
