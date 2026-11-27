"""Project Slack links: one channel per project and one project per channel."""

from __future__ import annotations

import uuid

import pytest

from omnigent.errors import OmnigentError
from omnigent.stores.project_slack_store import ChannelTakenError, ProjectSlackStore


def _claim(
    store: ProjectSlackStore, project_id: str, channel_id: str = "C0001", team: str = "T01"
):
    return store.claim(
        project_id,
        slack_team_id=team,
        channel_id=channel_id,
        channel_name="eng",
        destination={},
        owner_user_id="owner@example.com",
        actor_user_id="owner@example.com",
    )


def test_claim_is_unique_on_channel_and_project(db_uri: str) -> None:
    store = ProjectSlackStore(db_uri)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    binding = _claim(store, first)
    assert binding.state == "claimed"
    with pytest.raises(ChannelTakenError) as taken:
        _claim(store, second)
    assert taken.value.project_id == first
    # Channel IDs are unique across Slack, so naming another workspace doesn't help.
    with pytest.raises(ChannelTakenError):
        _claim(store, second, team="T02")
    # A link without a workspace ID still claims the channel; the watcher fills it in.
    third = store.claim(
        uuid.uuid4().hex,
        slack_team_id=None,
        channel_id="C0009",
        channel_name=None,
        destination={},
        owner_user_id="owner@example.com",
        actor_user_id="owner@example.com",
    )
    assert third.slack_team_id is None
    store.learn_team_id(third.id, "T09")
    store.learn_team_id(third.id, "T99")  # only the first report counts
    assert store.get(third.project_id).slack_team_id == "T09"


def test_relink_moves_the_one_binding_and_resets_state(db_uri: str) -> None:
    store = ProjectSlackStore(db_uri)
    project = uuid.uuid4().hex
    original = _claim(store, project)
    store.set_state(project, "active", actor_user_id="owner@example.com", automation_id="a-1")
    moved = _claim(store, project, channel_id="C0002")
    assert moved.id == original.id
    assert moved.state == "claimed"
    assert store.holder_of("C0001") is None
    assert store.holder_of("C0002") == project


def test_disable_keeps_the_claim_and_release_frees_it(db_uri: str) -> None:
    store = ProjectSlackStore(db_uri)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    _claim(store, first)
    store.set_state(first, "disabled", actor_user_id="owner@example.com")
    with pytest.raises(ChannelTakenError):
        _claim(store, second)
    assert store.release(first, actor_user_id="owner@example.com") is not None
    assert store.get(first) is None
    assert _claim(store, second).project_id == second
    actions = [entry.action for entry in store.audit(first)]
    assert {"linked", "disabled", "unlinked"} <= set(actions)


def test_set_state_without_link_is_not_found(db_uri: str) -> None:
    with pytest.raises(OmnigentError):
        ProjectSlackStore(db_uri).set_state(uuid.uuid4().hex, "active", actor_user_id=None)
