"""Forum state survives a real schema upgrade and compressed storage round trip."""

import uuid

import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, clear_engine_cache
from omnigent.server.forum.state import ForumState, Participant
from omnigent.server.forum.store import ForumStore


def test_forum_upgrade_roundtrip_and_downgrade(tmp_path):
    uri = f"sqlite:///{tmp_path / 'forum-migration.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "mm1a2b3c4d5e")
        assert "forums" not in sa.inspect(engine).get_table_names()
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "fm1a2b3c4d5e")
        forum_id = uuid.uuid4().hex
        state = ForumState(
            [
                Participant(uuid.uuid4().hex, "codex-native", "model", f"Agent {i}")
                for i in range(2)
            ]
        )
        ForumStore(uri).create(forum_id, state)
        assert ForumStore(uri).get(forum_id) == state
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "mm1a2b3c4d5e")
        assert "forums" not in sa.inspect(engine).get_table_names()
    finally:
        engine.dispose()
        clear_engine_cache()
