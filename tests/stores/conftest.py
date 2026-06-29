"""Shared fixtures for store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.canvas_store.sqlalchemy_store import SqlAlchemyCanvasStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore
from omnigent.stores.schedule_store.sqlalchemy_store import SqlAlchemyScheduleStore
from omnigent.stores.work_item_store.sqlalchemy_store import SqlAlchemyWorkItemStore


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    """
    :returns: A SqlAlchemyAgentStore backed by the test database.
    """
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def policy_store(db_uri: str) -> SqlAlchemyPolicyStore:
    """
    :returns: A SqlAlchemyPolicyStore backed by the test database.
    """
    return SqlAlchemyPolicyStore(db_uri)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    :returns: A SqlAlchemyConversationStore backed by the test database.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    """
    :returns: A LocalArtifactStore in a temp directory.
    """
    return LocalArtifactStore(str(tmp_path / "artifacts"))


@pytest.fixture()
def work_item_store(db_uri: str) -> SqlAlchemyWorkItemStore:
    """
    :returns: A SqlAlchemyWorkItemStore backed by the test database.
    """
    return SqlAlchemyWorkItemStore(db_uri)


@pytest.fixture()
def schedule_store(db_uri: str) -> SqlAlchemyScheduleStore:
    """
    :returns: A SqlAlchemyScheduleStore backed by the test database.
    """
    return SqlAlchemyScheduleStore(db_uri)


@pytest.fixture()
def canvas_store(db_uri: str) -> SqlAlchemyCanvasStore:
    """
    :returns: A SqlAlchemyCanvasStore backed by the test database.
    """
    return SqlAlchemyCanvasStore(db_uri)
