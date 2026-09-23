"""Saved inference configuration survives session lifecycle and storage reloads."""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from omnigent.db.db_models import (
    SqlAgent,
    SqlConversation,
    SqlConversationMetadata,
    current_workspace_id,
)
from omnigent.entities import Conversation
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    return request.getfixturevalue(request.param)


def _snapshot() -> dict[str, Any]:
    return {
        "revision": "synthetic-config-revision",
        "target_id": "sandbox:agent_sandbox",
        "runtime_config": {
            "providers": {
                "bifrost": {
                    "kind": "gateway",
                    "openai": {
                        "base_url": "https://bifrost.example/v1",
                        "api_key_ref": "env:INFERENCE_TEST_KEY",
                    },
                }
            },
            "inference": {
                "harnesses": {
                    "codex-native": {
                        "provider": "bifrost",
                        "default_model": "model-a",
                        "model_allowlist": ["model-a", "private.catalog/model-b"],
                    }
                }
            },
        },
    }


def _create_session(
    store: SqlAlchemyConversationStore,
    *,
    bundled: bool,
    snapshot: dict[str, Any] | None = None,
    parent_id: str | None = None,
) -> Conversation:
    if not bundled:
        return store.create_conversation(
            inference_snapshot=snapshot, parent_conversation_id=parent_id
        )
    agent_id = uuid.uuid4().hex
    return store.create_session_with_agent(
        agent_id=agent_id,
        agent_name="inference-snapshot-agent",
        agent_bundle_location=f"{agent_id}/bundle",
        agent_description=None,
        parent_conversation_id=parent_id,
        inference_snapshot=snapshot,
        reasoning_effort="high",
    ).conversation


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_snapshot_roundtrip_survives_reopening_store(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    snapshot = _snapshot()
    created = _create_session(store, bundled=bundled, snapshot=snapshot)
    assert created.inference_snapshot == snapshot

    reopened = SqlAlchemyConversationStore(
        store._engine.url.render_as_string(hide_password=False),
        store._conv_engine.url.render_as_string(hide_password=False),
    )
    fetched = reopened.get_conversation(created.id)
    assert fetched is not None
    assert fetched.inference_snapshot == snapshot
    if bundled:
        assert fetched.reasoning_effort == "high"


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_large_catalog_snapshot_uses_metadata_across_create_child_and_fork(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    snapshot = _snapshot()
    snapshot["catalog"] = {
        "models": [
            {
                "id": f"gateway-model-{index}",
                "label": f"Gateway model {index}",
                "provider": "bifrost",
            }
            for index in range(1500)
        ]
    }
    assert len(json.dumps(snapshot).encode()) > 65535
    source = _create_session(store, bundled=bundled, snapshot=snapshot)
    store.update_conversation(source.id, model_override="private.catalog/model-b")
    child = _create_session(store, bundled=not bundled, parent_id=source.id)
    fork = store.fork_conversation(source.id)

    for session in (source, child, fork):
        fetched = store.get_conversation(session.id)
        assert fetched is not None
        assert fetched.inference_snapshot == snapshot
        with store._engine.connect() as connection:
            saved = connection.scalar(
                sa.select(SqlConversationMetadata.inference_snapshot).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == session.id,
                )
            )
        assert saved is not None
        assert json.loads(saved) == snapshot
        with store._conv_engine.connect() as connection:
            overrides = connection.scalar(
                sa.select(SqlConversation.session_overrides).where(
                    SqlConversation.workspace_id == current_workspace_id(),
                    SqlConversation.id == session.id,
                )
            )
        assert len(overrides or "") <= 512
        assert "inference_snapshot" not in json.loads(overrides or "{}")


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_snapshot_isolated_from_input_edits_and_later_sessions(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    snapshot = _snapshot()
    original = copy.deepcopy(snapshot)
    created = _create_session(store, bundled=bundled, snapshot=snapshot)
    snapshot["revision"] = "edited-revision"
    snapshot["runtime_config"]["providers"]["bifrost"]["openai"]["base_url"] = (
        "https://other.example/v1"
    )
    snapshot["runtime_config"]["inference"]["harnesses"]["codex-native"]["model_allowlist"] = []
    later = _create_session(store, bundled=bundled, snapshot=snapshot)

    fetched = store.get_conversation(created.id)
    assert fetched is not None
    assert fetched.inference_snapshot == original
    assert created.inference_snapshot == original
    assert later.inference_snapshot == snapshot
    assert fetched.inference_snapshot is not None
    fetched.inference_snapshot["runtime_config"]["providers"].clear()
    reloaded = store.get_conversation(created.id)
    assert reloaded is not None
    assert reloaded.inference_snapshot == original


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_sessions_without_snapshots_preserve_legacy_behavior(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    bound = _create_session(store, bundled=bundled, snapshot=_snapshot())
    legacy = _create_session(store, bundled=bundled)
    child = _create_session(store, bundled=bundled, parent_id=legacy.id)
    assert bound.inference_snapshot is not None
    assert legacy.inference_snapshot is None
    assert child.inference_snapshot is None


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_children_and_grandchildren_inherit_saved_configuration(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    snapshot = _snapshot()
    parent = _create_session(store, bundled=bundled, snapshot=snapshot)
    child = _create_session(store, bundled=bundled, parent_id=parent.id)
    grandchild = _create_session(store, bundled=not bundled, parent_id=child.id)
    for conversation in (child, grandchild):
        assert conversation.inference_snapshot == snapshot
        assert conversation.root_conversation_id == parent.id
        fetched = store.get_conversation(conversation.id)
        assert fetched is not None
        assert fetched.inference_snapshot == snapshot
    assert child.inference_snapshot is not None
    child.inference_snapshot["runtime_config"]["providers"].clear()
    assert parent.inference_snapshot == snapshot
    assert grandchild.inference_snapshot == snapshot


@pytest.mark.parametrize("bundled", [False, True], ids=["existing-agent", "bundle"])
def test_explicit_child_snapshot_takes_precedence_over_parent(
    store: SqlAlchemyConversationStore, bundled: bool
) -> None:
    parent = _create_session(store, bundled=bundled, snapshot=_snapshot())
    child_snapshot = _snapshot()
    child_snapshot["revision"] = "explicit-child-config"
    child = _create_session(
        store, bundled=not bundled, snapshot=child_snapshot, parent_id=parent.id
    )
    assert child.inference_snapshot == child_snapshot
    assert child.inference_snapshot != parent.inference_snapshot
    fetched = store.get_conversation(child.id)
    assert fetched is not None
    assert fetched.inference_snapshot == child_snapshot


def test_snapshot_metadata_and_bundled_agent_roll_back_together(
    store: SqlAlchemyConversationStore,
) -> None:
    agent_id = uuid.uuid4().hex
    with pytest.raises(sa.exc.DBAPIError, match=r"(?i)check constraint"):
        store.create_session_with_agent(
            agent_id=agent_id,
            agent_name="invalid-metadata-agent",
            agent_bundle_location=f"{agent_id}/bundle",
            agent_description=None,
            inference_snapshot=_snapshot(),
            host_id=uuid.uuid4().hex,
            workspace=None,
        )

    with store._engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(SqlAgent)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(SqlConversationMetadata)) == 0
        )


@pytest.mark.parametrize("copy_model_settings", [False, True])
def test_fork_keeps_snapshot_even_when_resetting_model_choice(
    store: SqlAlchemyConversationStore, copy_model_settings: bool
) -> None:
    snapshot = _snapshot()
    source = store.create_conversation(inference_snapshot=snapshot)
    store.update_conversation(source.id, model_override="private.catalog/model-b")
    fork = store.fork_conversation(source.id, copy_model_settings=copy_model_settings)

    assert fork.inference_snapshot == snapshot
    assert fork.model_override == ("private.catalog/model-b" if copy_model_settings else None)
    assert fork.inference_snapshot is not None
    fork.inference_snapshot["runtime_config"]["providers"].clear()
    for conversation_id in (source.id, fork.id):
        fetched = store.get_conversation(conversation_id)
        assert fetched is not None
        assert fetched.inference_snapshot == snapshot


def test_model_edits_and_host_replacement_preserve_snapshot(
    store: SqlAlchemyConversationStore,
) -> None:
    snapshot = _snapshot()
    session = store.create_conversation(
        inference_snapshot=snapshot,
        host_id=uuid.uuid4().hex,
        runner_id="runner_initial",
        workspace="/tmp/inference-test",
    )
    store.update_conversation(
        session.id, model_override="private.catalog/model-b", reasoning_effort="high"
    )
    store.clear_host_binding(session.id)
    store.clear_runner_id(session.id)
    assert store.clear_model_override_if_matches(session.id, "private.catalog/model-b")

    fetched = store.get_conversation(session.id)
    assert fetched is not None
    assert fetched.inference_snapshot == snapshot
    assert fetched.model_override is None
    assert fetched.reasoning_effort == "high"
    assert fetched.host_id is None
    assert fetched.runner_id is None
