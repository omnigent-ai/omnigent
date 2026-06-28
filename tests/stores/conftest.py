"""Shared fixtures for store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.entity_group_store.sqlalchemy_store import SqlAlchemyEntityGroupStore
from omnigent.stores.entity_store.sqlalchemy_store import SqlAlchemyEntityStore
from omnigent.stores.job_store.sqlalchemy_store import SqlAlchemyJobStore
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


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
def job_store(db_uri: str) -> SqlAlchemyJobStore:
    """
    :returns: A SqlAlchemyJobStore backed by the test database.
    """
    return SqlAlchemyJobStore(db_uri)


@pytest.fixture()
def entity_store(db_uri: str) -> SqlAlchemyEntityStore:
    """
    :returns: A SqlAlchemyEntityStore backed by the test database.
    """
    return SqlAlchemyEntityStore(db_uri)


@pytest.fixture()
def entity_group_store(db_uri: str) -> SqlAlchemyEntityGroupStore:
    """
    :returns: A SqlAlchemyEntityGroupStore backed by the test database.
    """
    return SqlAlchemyEntityGroupStore(db_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    """
    :returns: A LocalArtifactStore in a temp directory.
    """
    return LocalArtifactStore(str(tmp_path / "artifacts"))
