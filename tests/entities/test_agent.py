"""Tests for agent entity dataclasses."""

from __future__ import annotations

from omnigent.entities.agent import Agent


def test_agent_minimal() -> None:
    agent = Agent(
        id="ag_abc123",
        created_at=1700000000,
        name="research-agent",
        bundle_location="ag_abc123/a1b2c3d4",
    )
    assert agent.id == "ag_abc123"
    assert agent.name == "research-agent"
    assert agent.version == 1
    assert agent.description is None
    assert agent.updated_at is None
    assert agent.session_id is None


def test_agent_full() -> None:
    agent = Agent(
        id="ag_xyz",
        created_at=1700000000,
        name="coder",
        bundle_location="ag_xyz/deadbeef",
        version=3,
        description="A coding agent",
        updated_at=1700001000,
        session_id="conv_session1",
    )
    assert agent.version == 3
    assert agent.description == "A coding agent"
    assert agent.updated_at == 1700001000
    assert agent.session_id == "conv_session1"


def test_agent_is_mutable() -> None:
    """Agent is a regular (non-frozen) dataclass — version bumps are allowed."""
    agent = Agent(
        id="ag_1",
        created_at=1,
        name="a",
        bundle_location="ag_1/hash",
    )
    agent.version = 2
    assert agent.version == 2


def test_agent_defaults_independent() -> None:
    """Each Agent gets independent default values."""
    a = Agent(id="ag_a", created_at=1, name="a", bundle_location="a/h")
    b = Agent(id="ag_b", created_at=1, name="b", bundle_location="b/h")
    a.description = "modified"
    assert b.description is None
