"""Tests for entity <-> ORM row converters (omnigent/db/converters.py).

The converter layer currently provides ``sql_agent_to_entity``.
Tests verify round-trip fidelity: entity -> ORM row -> entity, and
edge cases (None values, special characters).
"""

from __future__ import annotations

import time

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import Agent


def _now() -> int:
    return int(time.time())


class TestSqlAgentToEntity:
    """Tests for sql_agent_to_entity."""

    def test_basic_conversion(self) -> None:
        """All fields on the ORM row map to the corresponding entity fields."""
        row = SqlAgent(
            id="ag_abc123",
            created_at=1700000000,
            name="research-agent",
            bundle_location="ag_abc123/sha256hash",
            version=3,
            description="Does research",
            updated_at=1700001000,
            session_id="conv_xyz",
        )
        entity = sql_agent_to_entity(row)

        assert isinstance(entity, Agent)
        assert entity.id == "ag_abc123"
        assert entity.created_at == 1700000000
        assert entity.name == "research-agent"
        assert entity.bundle_location == "ag_abc123/sha256hash"
        assert entity.version == 3
        assert entity.description == "Does research"
        assert entity.updated_at == 1700001000
        assert entity.session_id == "conv_xyz"

    def test_nullable_fields_as_none(self) -> None:
        """Optional fields convert cleanly when they are None."""
        row = SqlAgent(
            id="ag_minimal",
            created_at=1700000000,
            name="minimal-agent",
            bundle_location="ag_minimal/hash",
            version=1,
            description=None,
            updated_at=None,
            session_id=None,
        )
        entity = sql_agent_to_entity(row)

        assert entity.description is None
        assert entity.updated_at is None
        assert entity.session_id is None

    def test_special_characters_in_fields(self) -> None:
        """Names and descriptions with unicode / special chars survive conversion."""
        row = SqlAgent(
            id="ag_unicode",
            created_at=1700000000,
            name="agent-with-emoji-\u2603",
            bundle_location="ag_unicode/hash",
            version=1,
            description="Handles \u00e9\u00e0\u00fc and newlines\nand tabs\t",
        )
        entity = sql_agent_to_entity(row)

        assert entity.name == "agent-with-emoji-\u2603"
        assert "\u00e9" in entity.description  # type: ignore[operator]
        assert "\n" in entity.description  # type: ignore[operator]

    def test_round_trip_entity_to_orm_to_entity(self) -> None:
        """Create an Agent entity, build an ORM row from it, convert back, and
        verify all fields match the original."""
        original = Agent(
            id="ag_roundtrip",
            created_at=1700000000,
            name="round-trip-agent",
            bundle_location="ag_roundtrip/abc123def456",
            version=5,
            description="A test agent for round-trip verification",
            updated_at=1700005000,
            session_id="conv_rt1",
        )

        # Entity -> ORM row (manual construction, mirroring what a store would do)
        row = SqlAgent(
            id=original.id,
            created_at=original.created_at,
            name=original.name,
            bundle_location=original.bundle_location,
            version=original.version,
            description=original.description,
            updated_at=original.updated_at,
            session_id=original.session_id,
        )

        # ORM row -> Entity (via the converter)
        result = sql_agent_to_entity(row)

        assert result.id == original.id
        assert result.created_at == original.created_at
        assert result.name == original.name
        assert result.bundle_location == original.bundle_location
        assert result.version == original.version
        assert result.description == original.description
        assert result.updated_at == original.updated_at
        assert result.session_id == original.session_id

    def test_round_trip_persisted_through_db(self, db_uri: str) -> None:
        """Full round-trip: entity -> ORM row -> persist -> load -> convert -> entity."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        original = Agent(
            id="ag_dbrt",
            created_at=_now(),
            name="db-round-trip",
            bundle_location="ag_dbrt/hash",
            version=2,
            description="Persisted and loaded back",
            updated_at=_now(),
            session_id=None,
        )

        row = SqlAgent(
            id=original.id,
            created_at=original.created_at,
            name=original.name,
            bundle_location=original.bundle_location,
            version=original.version,
            description=original.description,
            updated_at=original.updated_at,
            session_id=original.session_id,
        )
        with managed() as session:
            session.add(row)

        with managed() as session:
            loaded = session.get(SqlAgent, "ag_dbrt")
            assert loaded is not None
            result = sql_agent_to_entity(loaded)

        assert result.id == original.id
        assert result.created_at == original.created_at
        assert result.name == original.name
        assert result.bundle_location == original.bundle_location
        assert result.version == original.version
        assert result.description == original.description
        assert result.updated_at == original.updated_at
        assert result.session_id == original.session_id

    def test_version_default_after_persist(self, db_uri: str) -> None:
        """Version defaults to 1 when not explicitly set, after DB persistence."""
        engine = get_or_create_engine(db_uri)
        managed = make_managed_session_maker(engine)

        row = SqlAgent(
            id="ag_defver",
            created_at=1700000000,
            name="default-version",
            bundle_location="ag_defver/hash",
        )
        with managed() as session:
            session.add(row)

        with managed() as session:
            loaded = session.get(SqlAgent, "ag_defver")
            assert loaded is not None
            entity = sql_agent_to_entity(loaded)
            assert entity.version == 1

    def test_empty_string_description(self) -> None:
        """An empty-string description is preserved (not coerced to None)."""
        row = SqlAgent(
            id="ag_empty",
            created_at=1700000000,
            name="empty-desc",
            bundle_location="ag_empty/hash",
            version=1,
            description="",
        )
        entity = sql_agent_to_entity(row)
        assert entity.description == ""
