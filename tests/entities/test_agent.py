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


def _agent(**kwargs: object) -> Agent:
    base: dict[str, object] = {
        "id": "ag_x",
        "created_at": 1,
        "name": "a",
        "bundle_location": "ag_x/h",
    }
    base.update(kwargs)
    return Agent(**base)  # type: ignore[arg-type]


def test_expands_server_env_operator_template() -> None:
    """Operator-authored template (no session, no git) expands server env."""
    agent = _agent(session_id=None, git_url=None)
    assert agent.expands_server_env is True


def test_expands_server_env_session_scoped_denied() -> None:
    """A session-scoped (tenant-uploaded) agent must not expand server env."""
    agent = _agent(session_id="conv_1", git_url=None)
    assert agent.expands_server_env is False


def test_expands_server_env_git_imported_denied() -> None:
    """A git-imported template is untrusted repo config — no server-env expansion.

    This is the core of the fix: git agents persist as templates
    (``session_id is None``) but must NOT be granted the operator-template
    privilege of expanding ``${VAR}`` against the server process env, which
    would let a malicious repo's MCP ``env``/``headers`` exfiltrate server
    secrets.
    """
    agent = _agent(session_id=None, git_url="https://github.com/org/repo")
    assert agent.expands_server_env is False


def test_expands_server_env_git_and_session_denied() -> None:
    """Belt-and-suspenders: git provenance on a session-scoped row still denies."""
    agent = _agent(session_id="conv_1", git_url="https://github.com/org/repo")
    assert agent.expands_server_env is False
