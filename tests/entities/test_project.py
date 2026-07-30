"""Tests for the project entity dataclass."""

from __future__ import annotations

from omnigent.entities.project import Project

# ── Project ───────────────────────────────────────────


def test_project_construction() -> None:
    proj = Project(
        id="a" * 32,
        name="My Project",
        owner_user_id="alice@example.com",
        created_at=1700000000,
        updated_at=1700001000,
    )
    assert proj.id == "a" * 32
    assert proj.name == "My Project"
    assert proj.owner_user_id == "alice@example.com"
    assert proj.created_at == 1700000000
    assert proj.updated_at == 1700001000


def test_project_updated_at_defaults_to_none() -> None:
    """A freshly created project has no update timestamp yet."""
    proj = Project(
        id="b" * 32,
        name="Fresh",
        owner_user_id="alice@example.com",
        created_at=1700000000,
    )
    assert proj.updated_at is None


def test_project_owner_none_in_single_user_mode() -> None:
    """Single-user / OSS mode leaves ``owner_user_id`` as ``None``."""
    proj = Project(
        id="c" * 32,
        name="Solo",
        owner_user_id=None,
        created_at=1700000000,
    )
    assert proj.owner_user_id is None


def test_project_config_defaults_to_empty_dict() -> None:
    """A project with no stored defaults carries an empty config dict.

    Uses the default_factory, so two instances get independent dicts (no shared
    mutable default).
    """
    a = Project(id="d" * 32, name="A", owner_user_id=None, created_at=1)
    b = Project(id="e" * 32, name="B", owner_user_id=None, created_at=1)
    assert a.config == {}
    a.config["host_id"] = "x"
    assert b.config == {}  # not shared across instances


def test_project_carries_config() -> None:
    """A config object is stored on the entity."""
    proj = Project(
        id="f" * 32,
        name="Configured",
        owner_user_id=None,
        created_at=1,
        config={"host_id": "h", "workspace": "/w"},
    )
    assert proj.config == {"host_id": "h", "workspace": "/w"}


def test_project_equality() -> None:
    """Dataclasses support value-based equality."""
    a = Project(id="x" * 32, name="P", owner_user_id="u", created_at=1)
    b = Project(id="x" * 32, name="P", owner_user_id="u", created_at=1)
    assert a == b


def test_project_inequality_by_owner() -> None:
    """Two owners' identically named projects are distinct values."""
    a = Project(id="x" * 32, name="P", owner_user_id="alice", created_at=1)
    b = Project(id="x" * 32, name="P", owner_user_id="bob", created_at=1)
    assert a != b
