"""Agent store round-trips git_host_id and refresh updates it correctly."""

from __future__ import annotations

import pytest

from omnigent.db.utils import generate_agent_id
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore


@pytest.fixture
def store(tmp_path):
    return SqlAlchemyAgentStore(f"sqlite:///{tmp_path / 'agents.db'}")


def test_create_with_git_host_id_persists_it(store):
    aid = generate_agent_id()
    agent = store.create(
        aid,
        "git-host-agent",
        f"{aid}/deadbeef",
        git_url="https://github.com/org/repo",
        git_ref="main",
        git_commit="abc123",
        git_host_id="host_aabbccdd",
    )
    assert agent.git_host_id == "host_aabbccdd"
    assert store.get(aid).git_host_id == "host_aabbccdd"


def test_create_without_git_host_id_leaves_it_none(store):
    aid = generate_agent_id()
    agent = store.create(aid, "plain-agent", f"{aid}/feed")
    assert agent.git_host_id is None
    assert store.get(aid).git_host_id is None


def test_update_with_git_host_id_sets_it_and_bumps_version(store):
    aid = generate_agent_id()
    store.create(
        aid,
        "git-host-agent2",
        f"{aid}/old",
        git_url="https://github.com/org/repo",
        git_ref="main",
        git_commit="old_sha",
        git_host_id="host_old",
    )
    updated = store.update(aid, f"{aid}/new", git_commit="new_sha", git_host_id="host_new")
    assert updated.version == 2
    assert updated.bundle_location == f"{aid}/new"
    assert updated.git_commit == "new_sha"
    assert updated.git_host_id == "host_new"


def test_update_without_git_host_id_preserves_existing(store):
    aid = generate_agent_id()
    store.create(
        aid,
        "git-host-agent3",
        f"{aid}/old",
        git_url="https://github.com/org/repo",
        git_commit="sha1",
        git_host_id="host_keep",
    )
    updated = store.update(aid, f"{aid}/new", git_commit="sha2")
    assert updated.git_host_id == "host_keep"
