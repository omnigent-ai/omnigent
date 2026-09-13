"""Tests for entity <-> ORM row converters (omnigent/db/converters.py).

The converter layer currently provides ``sql_agent_to_entity``.
Tests verify round-trip fidelity: entity -> ORM row -> entity, and
edge cases (None values, special characters).
"""

from __future__ import annotations

import time

from omnigent.db.converters import sql_agent_to_entity
from omnigent.db.db_models import SqlAgent
from omnigent.db.enum_codecs import encode_agent_kind
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker
from omnigent.entities import Agent

AGENT_KIND_TEMPLATE = encode_agent_kind("template")
AGENT_KIND_SESSION = encode_agent_kind("session")


def _now() -> int:
    return int(time.time())


class TestSqlAgentToEntity:
    """Tests for sql_agent_to_entity."""

    def test_basic_conversion(self) -> None:
        """All fields on the ORM row map to the corresponding entity fields."""
        row = SqlAgent(
            id="104c4932179e16161e9ed9298fd5a3e2",
            created_at=1700000000,
            name="research-agent",
            bundle_location="ag_abc123/sha256hash",
            version=3,
            kind=AGENT_KIND_TEMPLATE,
            description="Does research",
            updated_at=1700001000,
        )
        entity = sql_agent_to_entity(row)

        assert isinstance(entity, Agent)
        assert entity.id == "104c4932179e16161e9ed9298fd5a3e2"
        assert entity.created_at == 1700000000
        assert entity.name == "research-agent"
        assert entity.bundle_location == "ag_abc123/sha256hash"
        assert entity.version == 3
        assert entity.description == "Does research"
        assert entity.updated_at == 1700001000
        assert entity.session_id is None  # template agents always have session_id=None

    def test_session_scoped_agent_passes_session_id(self) -> None:
        """session_id is forwarded for session-scoped agents."""
        row = SqlAgent(
            id="372d0296768feff7262c605c5553d1da",
            created_at=1700000000,
            name="session-agent",
            bundle_location="ag_sess/hash",
            version=1,
            kind=AGENT_KIND_SESSION,
        )
        entity = sql_agent_to_entity(row, session_id="12b8fd5b4413ededb99560e847b32b0e")
        assert entity.session_id == "12b8fd5b4413ededb99560e847b32b0e"

    def test_nullable_fields_as_none(self) -> None:
        """Optional fields convert cleanly when they are None."""
        row = SqlAgent(
            id="b9cd9ad3940ded42096b1b1ca99275c1",
            created_at=1700000000,
            name="minimal-agent",
            bundle_location="ag_minimal/hash",
            version=1,
            kind=AGENT_KIND_TEMPLATE,
            description=None,
            updated_at=None,
        )
        entity = sql_agent_to_entity(row)

        assert entity.description is None
        assert entity.updated_at is None
        assert entity.session_id is None

    def test_special_characters_in_fields(self) -> None:
        """Names and descriptions with unicode / special chars survive conversion."""
        row = SqlAgent(
            id="babb354a08c7d93e17d1d56ae9e8fa96",
            created_at=1700000000,
            name="agent-with-emoji-\u2603",
            bundle_location="ag_unicode/hash",
            version=1,
            kind=AGENT_KIND_TEMPLATE,
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
            id="90fe97c45ce0fd25c67a73f24b325697",
            created_at=1700000000,
            name="round-trip-agent",
            bundle_location="ag_roundtrip/abc123def456",
            version=5,
            description="A test agent for round-trip verification",
            updated_at=1700005000,
            session_id=None,
        )

        row = SqlAgent(
            id=original.id,
            created_at=original.created_at,
            name=original.name,
            bundle_location=original.bundle_location,
            version=original.version,
            kind=AGENT_KIND_TEMPLATE,
            description=original.description,
            updated_at=original.updated_at,
        )

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
            id="66f3422b1183ac0b04cb6f3f313c3f1e",
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
            kind=AGENT_KIND_TEMPLATE,
            description=original.description,
            updated_at=original.updated_at,
        )
        with managed() as session:
            session.add(row)

        with managed() as session:
            loaded = session.get(SqlAgent, (0, "66f3422b1183ac0b04cb6f3f313c3f1e"))
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
            id="5bc53358f0a0e7be94a234cb39730c38",
            created_at=1700000000,
            name="default-version",
            bundle_location="ag_defver/hash",
            kind=AGENT_KIND_TEMPLATE,
        )
        with managed() as session:
            session.add(row)

        with managed() as session:
            loaded = session.get(SqlAgent, (0, "5bc53358f0a0e7be94a234cb39730c38"))
            assert loaded is not None
            entity = sql_agent_to_entity(loaded)
            assert entity.version == 1

    def test_empty_string_description(self) -> None:
        """An empty-string description is preserved (not coerced to None)."""
        row = SqlAgent(
            id="2e58fb3f06227a85bf771cd4fedc5429",
            created_at=1700000000,
            name="empty-desc",
            bundle_location="ag_empty/hash",
            version=1,
            kind=AGENT_KIND_TEMPLATE,
            description="",
        )
        entity = sql_agent_to_entity(row)
        assert entity.description == ""
