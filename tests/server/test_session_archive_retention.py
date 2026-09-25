"""Inactive-session archive retention: policy, preview, and idempotent apply."""

from __future__ import annotations

import httpx
import pytest

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.runtime import pending_inputs
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.server.session_archive_retention import (
    RETAIN_LABEL_KEY,
    SessionArchiveRetentionPolicy,
    SessionArchiveRetentionService,
    SessionArchiveRetentionSweeper,
    classify_candidate,
    protection_rules,
)
from omnigent.stores.conversation_store import (
    ARCHIVED_AT_LABEL_KEY,
    ARCHIVED_BY_LABEL_KEY,
    ARCHIVED_BY_RETENTION_VALUE,
    PROJECT_LABEL_KEY,
    pinned_label_key,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

_DAY = 24 * 60 * 60


@pytest.fixture()
def stores(db_uri: str) -> tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore]:
    """Conversation and permission stores on one migrated database."""
    return SqlAlchemyConversationStore(db_uri), SqlAlchemyPermissionStore(db_uri)


@pytest.fixture()
def service(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
) -> SessionArchiveRetentionService:
    """Retention service that only archives sessions the caller owns."""
    conversation_store, _permissions = stores
    return SessionArchiveRetentionService(conversation_store, enforce_ownership=True)


def _own(
    permissions: SqlAlchemyPermissionStore,
    conversation_id: str,
    user_id: str = "alice",
) -> None:
    permissions.grant(user_id, conversation_id, LEVEL_OWNER)


def _enable(
    service: SessionArchiveRetentionService,
    *,
    days: int = 30,
    protect_pinned: bool = True,
    protect_shared: bool = True,
    protect_project: bool = True,
    protect_label_keys: tuple[str, ...] = (),
    user_id: str = "alice",
) -> SessionArchiveRetentionPolicy:
    return service.save_policy(
        user_id,
        SessionArchiveRetentionPolicy(
            enabled=True,
            inactive_days=days,
            protect_pinned=protect_pinned,
            protect_shared=protect_shared,
            protect_project=protect_project,
            protect_label_keys=protect_label_keys,
        ),
    )


def test_default_policy_is_disabled_and_rules_are_visible(
    service: SessionArchiveRetentionService,
) -> None:
    """Nothing is saved until a user configures a period, and the rules show first."""
    policy = service.get_policy("alice")
    assert policy.enabled is False
    assert policy.inactive_days is None
    assert policy.protect_pinned is True
    assert policy.protect_shared is True
    assert policy.protect_project is True
    rules = protection_rules(policy)
    assert rules["inactivity_basis"] == "updated_at"
    assert rules["always_exclude"] == ["active_work", "pending_user_input"]
    labeled = next(item for item in rules["protections"] if item["kind"] == "labeled")
    assert labeled["enabled"] is True
    assert labeled["label_keys"][0] == RETAIN_LABEL_KEY
    _policy, cutoff, decisions, _truncated = service.preview("alice")
    assert cutoff is None
    assert decisions == ()
    assert service.apply("alice").applied is False


def test_configure_update_and_disable_period(service: SessionArchiveRetentionService) -> None:
    """Users can set, change, and turn off the retention period."""
    saved = _enable(service, days=14)
    assert saved.enabled is True
    assert saved.inactive_days == 14

    updated = _enable(service, days=60, protect_pinned=False)
    assert updated.inactive_days == 60
    assert updated.protect_pinned is False

    disabled = service.save_policy(
        "alice",
        SessionArchiveRetentionPolicy(enabled=False, inactive_days=60),
    )
    assert disabled.enabled is False
    assert disabled.inactive_days == 60
    assert service.get_policy("alice").inactive_days == 60


def test_preview_does_not_archive_and_run_records_the_action(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """A dry run lists the session; a real run archives it once and keeps an audit."""
    conversation_store, permissions = stores
    conv = conversation_store.create_conversation(title="old chat")
    _own(permissions, conv.id)
    conversation_store.set_labels(conv.id, {"team": "ml"})
    conversation_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_keep",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "keep me"}],
                ),
            )
        ],
    )
    later = conv.updated_at + 40 * _DAY
    _enable(service, days=30)

    _policy, cutoff, decisions, _truncated = service.preview("alice", now=later)
    assert cutoff == later - 30 * _DAY
    assert [decision.candidate.id for decision in decisions if not decision.reasons] == [conv.id]
    assert conversation_store.get_conversation(conv.id).archived is False

    result = service.apply("alice", now=later)
    assert result.applied is True
    assert result.archived_ids == (conv.id,)
    archived = conversation_store.get_conversation(conv.id)
    assert archived is not None
    assert archived.archived is True
    assert archived.labels["team"] == "ml"
    assert archived.labels[ARCHIVED_BY_LABEL_KEY] == ARCHIVED_BY_RETENTION_VALUE
    archived_at = archived.labels[ARCHIVED_AT_LABEL_KEY]
    assert conversation_store.list_items(conv.id).data
    assert permissions.get("alice", conv.id) is not None

    policy = service.get_policy("alice")
    assert policy.last_run is not None
    assert policy.last_run["archived_session_ids"] == [conv.id]
    assert policy.last_run["ran_at"] == later

    again = service.apply("alice", now=later + _DAY)
    assert again.archived_ids == ()
    reread = conversation_store.get_conversation(conv.id)
    assert reread is not None
    assert reread.labels[ARCHIVED_AT_LABEL_KEY] == archived_at

    restored = conversation_store.update_conversation(conv.id, archived=False)
    assert restored is not None
    assert restored.archived is False
    assert ARCHIVED_AT_LABEL_KEY not in restored.labels
    assert ARCHIVED_BY_LABEL_KEY not in restored.labels
    assert restored.labels["team"] == "ml"
    assert conversation_store.list_items(conv.id).data


def test_recent_activity_is_not_eligible(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """Inactivity is the persisted updated_at timestamp, not creation time."""
    conversation_store, permissions = stores
    conv = conversation_store.create_conversation(title="fresh")
    _own(permissions, conv.id)
    _enable(service, days=30)
    result = service.apply("alice", now=conv.updated_at + _DAY)
    assert result.archived_ids == ()
    assert conversation_store.get_conversation(conv.id).archived is False


@pytest.mark.parametrize(
    ("status", "reason"),
    [("running", "active_work"), ("waiting", "pending_user_input")],
)
def test_live_status_excludes_the_session(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
    status: str,
    reason: str,
) -> None:
    """A running turn or a turn waiting on the user is never archived."""
    conversation_store, permissions = stores
    conv = conversation_store.create_conversation(title=status)
    _own(permissions, conv.id)
    conversation_store.set_session_live_status(conv.id, status)
    _enable(service, days=30)
    result = service.apply("alice", now=conv.updated_at + 40 * _DAY)
    assert result.archived_ids == ()
    assert result.skipped[0].reasons == (reason,)
    assert conversation_store.get_conversation(conv.id).archived is False


def test_pending_elicitation_and_composer_input_are_kept(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """Outstanding approvals and unconsumed composer messages stay active."""
    conversation_store, permissions = stores
    waiting_on_approval = conversation_store.create_conversation(title="approval")
    queued = conversation_store.create_conversation(title="queued")
    _own(permissions, waiting_on_approval.id)
    _own(permissions, queued.id)
    conversation_store.set_pending_elicitation_count(waiting_on_approval.id, 1)
    pending_inputs.record(queued.id, [{"type": "input_text", "text": "still typing"}])
    try:
        _enable(service, days=30)
        later = waiting_on_approval.updated_at + 40 * _DAY
        result = service.apply("alice", now=later)
        assert result.archived_ids == ()
        reasons = {decision.candidate.id: decision.reasons for decision in result.skipped}
        assert reasons[waiting_on_approval.id] == ("pending_user_input",)
        assert reasons[queued.id] == ("pending_user_input",)
    finally:
        pending_inputs.reset_for_tests()


def test_running_child_protects_the_parent(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """Active work anywhere in the spawn tree keeps the top-level session."""
    conversation_store, permissions = stores
    parent = conversation_store.create_conversation(title="parent")
    child = conversation_store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        title="worker:one",
    )
    _own(permissions, parent.id)
    conversation_store.set_session_live_status(child.id, "running")
    _enable(service, days=30)
    later = max(parent.updated_at, child.updated_at) + 40 * _DAY
    result = service.apply("alice", now=later)
    assert result.archived_ids == ()
    assert result.skipped[0].reasons == ("active_work",)


def test_pinned_shared_project_and_labeled_sessions_are_protected(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """Each protection is deterministic, and turning it off archives that session."""
    conversation_store, permissions = stores
    pinned = conversation_store.create_conversation(title="pinned")
    shared = conversation_store.create_conversation(title="shared")
    filed = conversation_store.create_conversation(title="filed")
    retained = conversation_store.create_conversation(title="retained")
    labeled = conversation_store.create_conversation(title="labeled")
    for conv in (pinned, shared, filed, retained, labeled):
        _own(permissions, conv.id)
    conversation_store.set_labels(pinned.id, {pinned_label_key("alice"): "10"})
    permissions.grant("bob", shared.id, LEVEL_READ)
    conversation_store.set_labels(filed.id, {PROJECT_LABEL_KEY: "launch"})
    conversation_store.set_labels(retained.id, {RETAIN_LABEL_KEY: "1"})
    conversation_store.set_labels(labeled.id, {"keep": "1"})
    _enable(service, protect_label_keys=("keep",))
    later = pinned.updated_at + 40 * _DAY

    _policy, _cutoff, decisions, _truncated = service.preview("alice", now=later)
    reasons = {decision.candidate.id: decision.reasons for decision in decisions}
    assert reasons[pinned.id] == ("pinned",)
    assert reasons[shared.id] == ("shared",)
    assert reasons[filed.id] == ("project",)
    assert reasons[retained.id] == ("labeled",)
    assert reasons[labeled.id] == ("labeled",)
    assert service.apply("alice", now=later).archived_ids == ()

    _enable(
        service,
        protect_pinned=False,
        protect_shared=False,
        protect_project=False,
        protect_label_keys=(),
    )
    result = service.apply("alice", now=later)
    archived_ids = set(result.archived_ids)
    assert pinned.id in archived_ids
    assert shared.id in archived_ids
    assert filed.id in archived_ids
    assert retained.id not in archived_ids
    kept = conversation_store.get_conversation(pinned.id)
    assert kept is not None
    assert kept.labels[pinned_label_key("alice")] == "10"


def test_other_owners_sessions_are_left_alone(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """A user's policy archives only the sessions that user owns."""
    conversation_store, permissions = stores
    mine = conversation_store.create_conversation(title="mine")
    theirs = conversation_store.create_conversation(title="theirs")
    _own(permissions, mine.id, "alice")
    _own(permissions, theirs.id, "bob")
    _enable(service, user_id="alice")
    later = mine.updated_at + 40 * _DAY
    result = service.apply("alice", now=later)
    assert result.archived_ids == (mine.id,)
    assert conversation_store.get_conversation(theirs.id).archived is False


def test_sweep_lease_is_single_writer_and_expires(
    stores: tuple[SqlAlchemyConversationStore, SqlAlchemyPermissionStore],
    service: SessionArchiveRetentionService,
) -> None:
    """One replica holds the sweep until the lease expires; a later sweep still runs."""
    conversation_store, permissions = stores
    conv = conversation_store.create_conversation(title="leased")
    _own(permissions, conv.id)
    _enable(service)
    later = conv.updated_at + 40 * _DAY
    sweeper = SessionArchiveRetentionSweeper(service, interval_s=60, clock=lambda: later)
    assert service.claim_sweep_lease("alice", later, 60) is True
    assert sweeper._claim_and_apply(0, "alice", later) == 0
    assert conversation_store.get_conversation(conv.id).archived is False
    assert sweeper._claim_and_apply(0, "alice", later + 60) == 1
    assert conversation_store.get_conversation(conv.id).archived is True


def test_classify_reason_order_is_stable() -> None:
    """Busy, pending, and every protection report in one fixed order."""
    from omnigent.stores.conversation_store import RetentionCandidate

    candidate = RetentionCandidate(
        id="conv_busy",
        title="busy",
        updated_at=10,
        project_id="proj",
        label_keys=(PROJECT_LABEL_KEY, RETAIN_LABEL_KEY, "omnigent.pinned.alice"),
        pinned=True,
        shared=True,
        tree_live_statuses=("running", "waiting"),
        tree_pending_elicitation_count=1,
        tree_ids=("conv_busy",),
    )
    policy = SessionArchiveRetentionPolicy(
        enabled=True,
        inactive_days=7,
        protect_pinned=True,
        protect_shared=True,
        protect_project=True,
    )
    assert classify_candidate(candidate, policy) == (
        "active_work",
        "pending_user_input",
        "pinned",
        "shared",
        "project",
        "labeled",
    )


@pytest.mark.asyncio
async def test_http_configure_preview_and_disable(client: httpx.AsyncClient, app) -> None:
    """The API configures, previews, disables, and refuses an enabled policy with no period."""
    initial = await client.get("/v1/session-archive-retention")
    assert initial.status_code == 200
    body = initial.json()
    assert body["enabled"] is False
    assert body["object"] == "session_archive_retention"
    assert body["rules"]["inactivity_basis"] == "updated_at"
    assert body["rules"]["always_exclude"] == ["active_work", "pending_user_input"]

    missing_period = await client.put(
        "/v1/session-archive-retention",
        json={"enabled": True},
    )
    assert missing_period.status_code == 422

    saved = await client.put(
        "/v1/session-archive-retention",
        json={"enabled": True, "inactive_days": 21, "protect_label_keys": ["keep"]},
    )
    assert saved.status_code == 200
    assert saved.json()["inactive_days"] == 21
    assert saved.json()["protect_label_keys"] == ["keep"]
    labeled = next(
        item for item in saved.json()["rules"]["protections"] if item["kind"] == "labeled"
    )
    assert "omnigent.retain" in labeled["label_keys"]
    assert "keep" in labeled["label_keys"]

    preview = await client.post("/v1/session-archive-retention/preview", json={})
    assert preview.status_code == 200
    assert preview.json()["dry_run"] is True
    assert preview.json()["inactive_days"] == 21

    disabled = await client.put(
        "/v1/session-archive-retention",
        json={"enabled": False, "inactive_days": 21},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    idle = await client.post("/v1/session-archive-retention/run")
    assert idle.status_code == 200
    assert idle.json()["applied"] is False
    assert idle.json()["archived_session_ids"] == []
    assert app.state.session_archive_retention.get_policy(None).enabled is False
