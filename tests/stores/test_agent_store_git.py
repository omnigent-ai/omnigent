"""Agent store round-trips git-source provenance and refresh updates the SHA."""

from __future__ import annotations

import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


@pytest.fixture
def store(tmp_path):
    return SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'agents.db'}")


def test_create_persists_git_fields(store):
    aid = generate_agent_id()
    agent = store.create(
        aid,
        "git-agent",
        f"{aid}/deadbeef",
        git_url="https://github.com/org/repo",
        git_ref="main",
        git_subpath=None,
        git_commit="abc123",
    )
    assert agent.git_url == "https://github.com/org/repo"
    assert agent.git_ref == "main"
    assert agent.git_subpath is None
    assert agent.git_commit == "abc123"
    assert store.get(aid).git_commit == "abc123"


def test_create_without_git_fields_leaves_them_none(store):
    aid = generate_agent_id()
    agent = store.create(aid, "plain", f"{aid}/feed")
    assert agent.git_url is None and agent.git_commit is None


def test_update_bumps_version_and_sets_commit(store):
    aid = generate_agent_id()
    store.create(aid, "git-agent", f"{aid}/old", git_url="u", git_ref="main", git_commit="old")
    updated = store.update(aid, f"{aid}/new", git_commit="new")
    assert updated.version == 2
    assert updated.bundle_location == f"{aid}/new"
    assert updated.git_commit == "new"
    assert updated.git_url == "u"  # provenance preserved


def test_update_without_commit_preserves_existing(store):
    aid = generate_agent_id()
    store.create(aid, "git-agent", f"{aid}/old", git_url="u", git_commit="keep")
    updated = store.update(aid, f"{aid}/new")
    assert updated.git_commit == "keep"
