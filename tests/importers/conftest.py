"""Shared fixtures for importer tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixtures_dir() -> Path:
    """:returns: The directory holding the sample transcript fixtures."""
    return FIXTURES_DIR


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """:returns: A SqlAlchemyConversationStore backed by the test database."""
    return SqlAlchemyConversationStore(db_uri)
