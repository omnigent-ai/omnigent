"""Portable session intent survives storage reopen, forks and host replacement."""

import json
from uuid import uuid4

import pytest
import sqlalchemy as sa

from omnigent.db.db_models import SqlConversation, SqlConversationMetadata
from omnigent.preference_startup import PreferenceLaunch
from omnigent.preference_sync import (
    PreferenceNamespace,
    SharedPreferenceEntry,
    SharedPreferenceKey,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.preferences import preference_snapshot


@pytest.mark.parametrize("store_name", ["conversation_store", "split_db_conversation_store"])
@pytest.mark.parametrize("bundled", [False, True])
def test_saved_preferences_survive_session_lifecycle(request, tmp_path, store_name, bundled):
    store = request.getfixturevalue(store_name)
    path = preference_snapshot(
        tmp_path / "handoff",
        identity=PreferenceNamespace(authority_id="test", workspace_id="0", user_id="alice"),
        shared=[
            SharedPreferenceEntry(
                key=SharedPreferenceKey(kind="model", name="model"), version=1, value="original"
            )
        ],
        machine=[
            SharedPreferenceEntry(
                key=SharedPreferenceKey(kind="harness_args", name="pi-native"), version=0, value=[]
            )
        ],
    )
    snapshot = json.loads(path.read_text())

    def create(parent=None, value=None):
        kwargs = {"parent_conversation_id": parent, "preference_snapshot": value}
        if not bundled:
            return store.create_conversation(**kwargs)
        agent = uuid4().hex
        return store.create_session_with_agent(
            agent_id=agent,
            agent_name="preferences",
            agent_bundle_location=f"{agent}/bundle",
            agent_description=None,
            **kwargs,
        ).conversation

    source = create(value=snapshot)
    child = create(parent=source.id)
    fork = store.fork_conversation(source.id, copy_model_settings=False)
    store.update_conversation(source.id, model_override="session-only")
    store.clear_host_binding(source.id)
    store.clear_runner_id(source.id)
    snapshot["shared"]["entries"][0]["value"] = "new-default"
    newer = create(value=snapshot)
    assert newer.preference_snapshot != source.preference_snapshot
    reopened = SqlAlchemyConversationStore(
        store._engine.url.render_as_string(hide_password=False),
        store._conv_engine.url.render_as_string(hide_password=False),
    )
    for session in (source, child, fork):
        saved = reopened.get_conversation(session.id)
        assert saved.preference_snapshot == json.loads(path.read_text())
        assert (
            PreferenceLaunch.model_validate(saved.preference_snapshot)
            .resolve({}, {})
            .config["model"]
            == "original"
        )
        with store._engine.connect() as conn:
            binary = conn.scalar(
                sa.select(
                    sa.type_coerce(SqlConversationMetadata.preference_snapshot, sa.LargeBinary)
                ).where(SqlConversationMetadata.id == session.id)
            )
            assert binary.startswith(b"\x00\x01")
        with store._conv_engine.connect() as conn:
            overrides = conn.scalar(
                sa.select(SqlConversation.session_overrides).where(
                    SqlConversation.id == session.id
                )
            )
            assert "preference_snapshot" not in json.loads(overrides or "{}")
    legacy = create()
    assert legacy.preference_snapshot is None
    assert create(parent=legacy.id).preference_snapshot is None
    assert store.fork_conversation(legacy.id).preference_snapshot is None


def test_invalid_preference_snapshot_creates_no_rows(conversation_store):
    store = conversation_store
    with pytest.raises(ValueError):
        store.create_conversation(preference_snapshot={"commands": ["must not be portable"]})
    assert store.list_conversations().data == []


def test_oversized_preference_snapshot_creates_no_rows(conversation_store, tmp_path):
    import random

    from omnigent.errors import OmnigentError

    randomizer = random.Random(0)
    path = preference_snapshot(
        tmp_path,
        identity=PreferenceNamespace(authority_id="test", workspace_id="0", user_id="alice"),
        shared=[
            SharedPreferenceEntry(
                key=SharedPreferenceKey(kind="harness_args", name=f"harness-{index}"),
                version=1,
                value=[randomizer.randbytes(3000).hex()],
            )
            for index in range(32)
        ],
    )
    with pytest.raises(OmnigentError, match="compressed storage limit"):
        conversation_store.create_conversation(preference_snapshot=json.loads(path.read_text()))
    assert conversation_store.list_conversations().data == []
