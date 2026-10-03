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
            ("workspace_id", "delivered", "not_before", "lease_until"),
            ("expires_at", "workspace_id", "id"),
            ("workspace_id", "installation_id"),
            ("workspace_id", "user_id"),
            ("delivered", "workspace_id"),
        }
        assert {
            tuple(index["column_names"]) for index in inspector.get_indexes("mobile_push_devices")
        } >= {("expires_at", "workspace_id", "installation_id")}
        command.downgrade(config, "mm1a2b3c4d5e")
        assert "mobile_push_outbox" not in sa.inspect(connection).get_table_names()
        command.upgrade(config, "head")
