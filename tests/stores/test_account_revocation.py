"""Revocation ordering against real database transactions (including the PG lane)."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest

from omnigent.db.account_authority import account_authority_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import accounts_store as accounts_module
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.device_grant_store import DeviceGrantStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


def test_account_generation_survives_delete_and_rejects_old_writes(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    old = accounts.create_user_with_password("alice", "test-password-hash")
    assert old.account_generation is not None
    assert accounts.delete_user("alice") is True
    assert accounts.get_user("alice") is None
    with pytest.raises(OmnigentError, match="revoked"):
        permissions.ensure_user("alice")
    new = accounts.create_user_with_password("alice", "replacement-password-hash")
    assert new.account_generation and new.account_generation != old.account_generation
    with account_authority_scope("alice", old.account_generation):
        for write in (
            lambda: permissions.ensure_user("alice"),
            lambda: permissions.grant("alice", uuid.uuid4().hex, 3),
            lambda: HostStore(db_uri).upsert_on_connect(uuid.uuid4().hex, "laptop", "alice"),
            lambda: DeviceGrantStore(db_uri).create_redeemed_grant(
                "late-grant",
                user_id="alice",
                client_id="omnigent-cli",
                refresh_token_hash="hash",
                created_at=int(time.time()),
            ),
            lambda: SqlAlchemyScheduledTaskStore(db_uri).create(
                uuid.uuid4().hex, "job", "hello", "FREQ=DAILY", "alice", uuid.uuid4().hex, "UTC"
            ),
            lambda: SqlAlchemyConversationStore(db_uri).add_daily_cost("alice", "2026-09-18", 1.0),
            lambda: SqlAlchemyConversationStore(db_uri).set_daily_ask_approved(
                "alice", "2026-09-18", 0.5
            ),
        ):
            with pytest.raises(OmnigentError, match="revoked"):
                write()
    with account_authority_scope("alice", new.account_generation):
        permissions.ensure_user("alice")
        assert HostStore(db_uri).upsert_on_connect(uuid.uuid4().hex, "laptop", "alice")


def test_host_replacement_serializes_with_account_deletion(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    hosts = HostStore(db_uri)
    accounts.create_user_with_password("alice", "test-password-hash")
    host_id = uuid.uuid4().hex
    host = hosts.register_managed_host(
        host_id=host_id,
        name="managed",
        user_id="alice",
        token="old-token",
        provider="modal",
        sandbox_id="old-sandbox",
        token_expires_at=int(time.time()) + 3600,
    )
    assert hosts.detach_stale_managed_sandbox(
        host_id, sandbox_id="old-sandbox", expected_updated_at=host.updated_at
    )
    assert hosts.mark_sandbox_terminated(host_id, sandbox_id="old-sandbox")
    reached, resume, replacement_started = Event(), Event(), Event()
    cleanup = accounts_module._revoke_durable_authority

    def pause_cleanup(*args, **kwargs):
        reached.set()
        assert resume.wait(10)
        return cleanup(*args, **kwargs)

    def replace():
        replacement_started.set()
        return hosts.replace_managed_host_sandbox(
            host_id=host_id,
            user_id="alice",
            token="replacement",
            provider="modal",
            sandbox_id="new-sandbox",
            token_expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(accounts_module, "_revoke_durable_authority", pause_cleanup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(accounts.delete_user, "alice")
        try:
            assert reached.wait(10)
            replacing = pool.submit(replace)
            assert replacement_started.wait(10)
            with pytest.raises(TimeoutError):
                replacing.result(timeout=0.1)
        finally:
            resume.set()
        assert deleting.result(timeout=10) is True
        with pytest.raises(OmnigentError, match="revoked"):
            replacing.result(timeout=10)
    assert hosts.get_host(host_id) is None
    assert hosts.resolve_launch_token(host_id, "replacement") is None


def test_deletion_retains_sandbox_cleanup_and_invalidates_magic_links(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    hosts = HostStore(db_uri)
    accounts.create_user_with_password("alice", "test-password-hash")
    host_id = uuid.uuid4().hex
    hosts.register_managed_host(
        host_id=host_id,
        name="managed",
        user_id="alice",
        token="token",
        provider="modal",
        sandbox_id="sandbox",
        token_expires_at=int(time.time()) + 3600,
    )
    accounts.create_token(
        "magic",
        kind="magic",
        user_id="alice",
        created_by=None,
        created_at=0,
        expires_at=2000000000,
    )
    assert accounts.delete_user("alice") is True
    assert hosts.get_host(host_id) is None
    assert hosts.resolve_launch_token(host_id, "token") is None
    assert hosts.mark_sandbox_terminated(host_id, sandbox_id="sandbox")
    accounts.create_user_with_password("alice", "new-password-hash")
    assert accounts.redeem_token("magic", kind="magic", now_epoch_seconds=1) is None


def test_username_reuse_does_not_inherit_connections_projects_or_spending(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
    from tests.server.test_credential_store import _store

    accounts = SqlAlchemyAccountStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    credentials = _store(db_uri)
    old = accounts.create_user_with_password("alice", "test-password-hash")
    costs = SqlAlchemyConversationStore(db_uri)
    costs.add_daily_cost("alice", "2026-09-18", 10.0)
    costs.set_daily_ask_approved("alice", "2026-09-18", 5.0)
    costs.add_daily_cost("other", "2026-09-18", 3.0)
    project = projects.create(uuid.uuid4().hex, "private", "alice")
    projects.save_order([project.id], user_id="alice")
    credentials.upsert("alice", "github", secret={"access_token": "local-fake-token"}, metadata={})
    assert accounts.delete_user("alice")
    accounts.create_user_with_password("alice", "replacement-password")
    assert costs.get_daily_cost_state("alice", "2026-09-18") == {
        "cost_usd": 0.0,
        "ask_approved_usd": 0.0,
    }
    assert costs.get_daily_cost("other", "2026-09-18") == 3.0
    assert projects.list(user_id="alice") == []
    assert projects.get_order(user_id="alice") is None
    assert credentials.get("alice", "github", with_secret=True) is None
    with account_authority_scope("alice", old.account_generation):
        with pytest.raises(OmnigentError, match="revoked"):
            credentials.upsert(
                "alice", "github", secret={"access_token": "late-token"}, metadata={}
            )
        with pytest.raises(OmnigentError, match="revoked"):
            projects.create(uuid.uuid4().hex, "late-project", "alice")
        for order in ([], None):
            with pytest.raises(OmnigentError, match="revoked"):
                projects.save_order(order, user_id="alice")


def test_deletion_removes_all_preferences_only_for_user_in_workspace(db_uri: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, workspace_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    for workspace_id in (101, 102):
        with workspace_scope(workspace_id):
            for user_id in ("alice", "bob"):
                accounts.create_user_with_password(user_id, "test-password-hash")
    with Session(accounts._engine) as session:
        session.add_all(
            SqlPreference(workspace_id=workspace_id, user_id=user_id, key=key, value="{}")
            for workspace_id in (101, 102)
            for user_id in ("alice", "bob")
            for key in ("project_order", "theme")
        )
        session.commit()

    with workspace_scope(101):
        assert accounts.delete_user("alice") is True
        accounts.create_user_with_password("alice", "replacement-password-hash")
    with Session(accounts._engine) as session:
        remaining = set(
            session.execute(
                select(SqlPreference.workspace_id, SqlPreference.user_id, SqlPreference.key)
            ).all()
        )
    assert remaining == {
        (workspace_id, user_id, key)
        for workspace_id, user_id in ((101, "bob"), (102, "alice"), (102, "bob"))
        for key in ("project_order", "theme")
    }


def test_cleanup_failure_rolls_back_revocation(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.stores import host_store as hosts_module

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    hosts = HostStore(db_uri)
    costs = SqlAlchemyConversationStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    session_id, host_id = uuid.uuid4().hex, uuid.uuid4().hex
    permissions.grant("alice", session_id, 3)
    hosts.upsert_on_connect(host_id, "laptop", "alice")
    costs.add_daily_cost("alice", "2026-09-18", 1.0)
    costs.set_daily_ask_approved("alice", "2026-09-18", 0.5)

    def fail_cleanup(*args):
        raise RuntimeError("test cleanup failure")

    monkeypatch.setattr(hosts_module, "delete_host_in_session", fail_cleanup)
    with pytest.raises(RuntimeError, match="test cleanup failure"):
        accounts.delete_user("alice")
    assert accounts.get_user("alice") == account
    assert permissions.get("alice", session_id) is not None
    assert hosts.get_host(host_id) is not None
    assert costs.get_daily_cost_state("alice", "2026-09-18") == {
        "cost_usd": 1.0,
        "ask_approved_usd": 0.5,
    }


@pytest.mark.parametrize("operation", ["cost", "approval", "subagent_cost"])
@pytest.mark.parametrize("account_owner", [False, True])
def test_background_budget_write_pins_owner_registration(
    db_uri, monkeypatch, operation, account_owner
):
    from omnigent.db.utils import now_epoch, utc_day
    from omnigent.policies.schema import USER_DAILY_ASK_APPROVED_STATE_KEY
    from omnigent.runtime.policies.engine import PolicyEngine
    from omnigent.server.routes._sessions.helpers import _record_daily_cost
    from omnigent.spec.types import StateUpdate, StateUpdateAction

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    if account_owner:
        accounts.create_user_with_password("alice", "old-password")
    else:
        permissions.ensure_user("alice")
    root = conversations.create_conversation()
    permissions.grant("alice", root.id, 4)
    conversation = (
        conversations.create_conversation(parent_conversation_id=root.id)
        if operation == "subagent_cost"
        else root
    )
    read_owner = conversations.get_session_owner_authority
    captured = []

    def replace_after_lookup(*args, **kwargs):
        owner = read_owner(*args, **kwargs)
        if owner is not None:
            captured.append(owner)
            assert accounts.delete_user("alice")
            accounts.create_user_with_password("alice", "new-password")
        return owner

    monkeypatch.setattr(conversations, "get_session_owner_authority", replace_after_lookup)
    with account_authority_scope(None, None):
        with pytest.raises(OmnigentError, match="revoked") as error:
            if operation == "approval":
                engine = PolicyEngine(
                    policies=[],
                    label_defs={},
                    ask_timeout=30,
                    conversation_id=conversation.id,
                    initial_labels={},
                    conversation_store=conversations,
                )
                engine.apply_state_updates(
                    [
                        StateUpdate(
                            key=USER_DAILY_ASK_APPROVED_STATE_KEY,
                            action=StateUpdateAction.SET,
                            value=0.5,
                        )
                    ]
                )
            else:
                _record_daily_cost(conversation, 1.0, conversations)
    assert error.value.code == ErrorCode.CONFLICT
    assert len(captured) == 1
    assert (captured[0].generation is not None) == account_owner
    assert conversations.get_daily_cost_state("alice", utc_day(now_epoch())) == {
        "cost_usd": 0.0,
        "ask_approved_usd": 0.0,
    }


@pytest.mark.parametrize("writer_name", ["add_daily_cost", "set_daily_ask_approved"])
@pytest.mark.parametrize("delete_first", [False, True])
def test_budget_writes_serialize_with_deletion(db_uri, monkeypatch, writer_name, delete_first):
    from omnigent.stores.conversation_store import sqlalchemy_store as conversation_module

    accounts = SqlAlchemyAccountStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    account = accounts.create_user_with_password("alice", "old-password")
    reached, resume, second_started = Event(), Event(), Event()
    original = (
        accounts_module._revoke_durable_authority
        if delete_first
        else conversation_module.require_active_account
    )

    def paused_lock(*args, **kwargs):
        result = original(*args, **kwargs)
        reached.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(
        accounts_module if delete_first else conversation_module,
        "_revoke_durable_authority" if delete_first else "require_active_account",
        paused_lock,
    )

    def write():
        with account_authority_scope("alice", account.account_generation):
            getattr(conversations, writer_name)("alice", "2026-09-18", 1.0)

    def second():
        second_started.set()
        return write() if delete_first else accounts.delete_user("alice")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(accounts.delete_user, "alice") if delete_first else pool.submit(write)
        try:
            assert reached.wait(10)
            waiting = pool.submit(second)
            assert second_started.wait(10)
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.1)
        finally:
            resume.set()
        first.result(timeout=10)
        if delete_first:
            with pytest.raises(OmnigentError, match="revoked"):
                waiting.result(timeout=10)
        else:
            assert waiting.result(timeout=10) is True
    assert accounts.get_user("alice") is None
    assert conversations.get_daily_cost_state("alice", "2026-09-18") == {
        "cost_usd": 0.0,
        "ask_approved_usd": 0.0,
    }


def test_unbound_saved_authority_cannot_acquire_new_account(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    accounts.create_user_with_password("alice", "test-password-hash")
    with account_authority_scope("alice", None):
        with pytest.raises(OmnigentError, match="revoked"):
            permissions.ensure_user("alice")


def test_host_identity_changes_preserve_account_generation(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    hosts = HostStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    original_id, rotated_id = uuid.uuid4().hex, uuid.uuid4().hex
    hosts.upsert_on_connect(original_id, "laptop", "local")
    with account_authority_scope("alice", account.account_generation):
        claimed = hosts.upsert_on_connect(original_id, "laptop", "alice", allow_host_id_reown=True)
        assert claimed.account_generation == account.account_generation
        rotated = hosts.upsert_on_connect(rotated_id, "laptop", "alice")
    assert rotated.account_generation == account.account_generation
    assert hosts.get_host(rotated_id).account_generation == account.account_generation


@pytest.mark.parametrize("revoked_owner", ["alice", "z-host-owner"])
def test_runner_issuance_serializes_with_deletion(db_uri: str, revoked_owner) -> None:
    from omnigent.db.account_authority import account_generation
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    accounts.create_user_with_password("z-host-owner", "host-hash")
    host_id = uuid.uuid4().hex
    HostStore(db_uri).upsert_on_connect(host_id, "laptop", "z-host-owner")
    runner_id = uuid.uuid4().hex
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        host_id=host_id, workspace="/tmp/workspace", runner_id=runner_id
    )
    SqlAlchemyPermissionStore(db_uri).grant("alice", conv.id, LEVEL_OWNER)
    reached, resume, deletion_started = Event(), Event(), Event()

    def issue(owner):
        assert owner == "alice"
        assert account_generation(owner) == account.account_generation
        reached.set()
        assert resume.wait(10)
        return "issued-under-lock"

    def delete():
        deletion_started.set()
        return accounts.delete_user(revoked_owner)

    with ThreadPoolExecutor(max_workers=2) as pool:
        issuance = pool.submit(accounts.with_runner_authority, runner_id, issue)
        try:
            assert reached.wait(10)
            deletion = pool.submit(delete)
            assert deletion_started.wait(10)
            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.2)
        finally:
            resume.set()
        assert issuance.result(timeout=10) == "issued-under-lock"
        assert deletion.result(timeout=10) is True
    assert accounts.with_runner_authority(runner_id, issue) is None


def test_concurrent_admin_deletions_preserve_an_active_admin(db_uri: str) -> None:
    from threading import Barrier

    accounts = SqlAlchemyAccountStore(db_uri)
    alice = accounts.create_user_with_password("alice", "hash", is_admin=True)
    bob = accounts.create_user_with_password("bob", "hash", is_admin=True)
    barrier = Barrier(2)

    def delete(actor, target):
        with account_authority_scope(actor.id, actor.account_generation):
            barrier.wait(timeout=10)
            try:
                return accounts.delete_user(target.id)
            except OmnigentError:
                return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(delete, alice, bob)
        b = pool.submit(delete, bob, alice)
        assert sorted([a.result(timeout=10), b.result(timeout=10)]) == [False, True]
    assert sum(accounts.is_admin(user) for user in ("alice", "bob")) == 1


def test_launch_admission_requires_binding_unless_initial_bind(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    hosts = HostStore(db_uri)
    host_id = uuid.uuid4().hex
    hosts.upsert_on_connect(host_id, "laptop", "alice")
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()
    with pytest.raises(OmnigentError, match="bound"):
        hosts.admit_launch(host_id, conv.id, "alice", account.account_generation)
    hosts.admit_launch(host_id, conv.id, "alice", account.account_generation, allow_unbound=True)
    conversations.set_host_id(conv.id, host_id, workspace="/tmp/workspace")
    hosts.admit_launch(host_id, conv.id, "alice", account.account_generation)
    assert accounts.delete_user("alice") is True
    with pytest.raises(OmnigentError, match="revoked"):
        hosts.admit_launch(
            host_id, conv.id, "alice", account.account_generation, allow_unbound=True
        )


def test_launch_admission_serializes_with_session_owner_deletion(db_uri, monkeypatch):
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores import host_store as hosts_module
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    owner = accounts.create_user_with_password("a-session-owner", "hash")
    host_owner = accounts.create_user_with_password("z-host-owner", "hash")
    hosts = HostStore(db_uri)
    host = hosts.upsert_on_connect(uuid.uuid4().hex, "host", host_owner.id)
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation(
        host_id=host.host_id, workspace="/tmp"
    )
    SqlAlchemyPermissionStore(db_uri).grant(owner.id, conversation.id, LEVEL_OWNER)
    checked, resume, deleting = Event(), Event(), Event()
    require = hosts_module.require_active_account

    def pause_under_account_locks(*args, **kwargs):
        result = require(*args, **kwargs)
        checked.set()
        assert resume.wait(10)
        return result

    def admit():
        hosts.admit_launch(
            host.host_id,
            conversation.id,
            host_owner.id,
            host_owner.account_generation,
            require_account_owner=True,
        )

    def delete():
        deleting.set()
        return accounts.delete_user(owner.id)

    monkeypatch.setattr(hosts_module, "require_active_account", pause_under_account_locks)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(admit)
        try:
            assert checked.wait(10)
            deletion = pool.submit(delete)
            assert deleting.wait(10)
            with pytest.raises(TimeoutError):
                deletion.result(timeout=0.2)
        finally:
            resume.set()
        assert admission.result(timeout=10) is None
        assert deletion.result(timeout=10) is True
    with pytest.raises(OmnigentError, match="active account owner"):
        admit()


@pytest.mark.parametrize("change", ["registration", "permission"])
def test_launch_admission_rechecks_session_owner_snapshot(db_uri, monkeypatch, change):
    from omnigent.db import account_authority
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores import host_store as hosts_module
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    owner = accounts.create_user_with_password("a-session-owner", "hash")
    host_owner = accounts.create_user_with_password("z-host-owner", "hash")
    hosts = HostStore(db_uri)
    host = hosts.upsert_on_connect(uuid.uuid4().hex, "host", host_owner.id)
    conversation = SqlAlchemyConversationStore(db_uri).create_conversation(
        host_id=host.host_id, workspace="/tmp"
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(owner.id, conversation.id, LEVEL_OWNER)
    locked = []
    lock = account_authority.lock_account

    def record_lock(session, user_id):
        locked.append(user_id)
        return lock(session, user_id)

    def admit():
        hosts.admit_launch(
            host.host_id,
            conversation.id,
            host_owner.id,
            host_owner.account_generation,
            require_account_owner=True,
        )

    monkeypatch.setattr(account_authority, "lock_account", record_lock)
    admit()
    assert locked == [owner.id, host_owner.id]
    if accounts._engine.dialect.name == "sqlite":
        pytest.skip("SQLite already holds its database-wide writer lock at the snapshot")
    snapshot_read, resume = Event(), Event()
    require = hosts_module.require_active_account

    def pause_before_locks(*args, **kwargs):
        snapshot_read.set()
        assert resume.wait(10)
        return require(*args, **kwargs)

    monkeypatch.setattr(hosts_module, "require_active_account", pause_before_locks)
    with ThreadPoolExecutor(max_workers=1) as pool:
        admission = pool.submit(admit)
        try:
            assert snapshot_read.wait(10)
            monkeypatch.setattr(hosts_module, "require_active_account", require)
            if change == "registration":
                assert accounts.delete_user(owner.id)
                accounts.create_user_with_password(owner.id, "replacement")
            else:
                permissions.revoke(owner.id, conversation.id)
        finally:
            resume.set()
        with pytest.raises(OmnigentError) as caught:
            admission.result(timeout=10)
        assert caught.value.code == ErrorCode.UNAUTHORIZED


def test_transfer_admission_checks_source_and_saved_generation(db_uri: str) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    hosts = HostStore(db_uri)
    source, destination, other = (uuid.uuid4().hex for _ in range(3))
    for host_id in (source, destination, other):
        hosts.upsert_on_connect(host_id, host_id, "alice")
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, source, workspace="/tmp/source")
    with pytest.raises(OmnigentError, match="bound"):
        hosts.admit_launch(destination, conv.id, "alice", account.account_generation)
    with pytest.raises(OmnigentError, match="bound"):
        hosts.admit_launch(
            destination, conv.id, "alice", account.account_generation, allow_unbound=True
        )

    def admit_transfer():
        hosts.admit_launch(
            destination,
            conv.id,
            "alice",
            account.account_generation,
            allow_unbound=True,
            transfer_from_host_id=source,
        )

    admit_transfer()
    assert conversations.get_conversation(conv.id).host_id == source
    conversations.set_host_id(conv.id, other)
    with pytest.raises(OmnigentError, match="bound"):
        admit_transfer()
    conversations.set_host_id(conv.id, source)
    assert accounts.delete_user("alice") is True
    accounts.create_user_with_password("alice", "replacement-password-hash")
    hosts.upsert_on_connect(destination, "new-laptop", "alice")
    with pytest.raises(OmnigentError, match="revoked"):
        admit_transfer()


def test_transfer_admission_waits_for_account_deletion(db_uri, monkeypatch):
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    account = accounts.create_user_with_password("alice", "test-password-hash")
    hosts = HostStore(db_uri)
    source, destination = uuid.uuid4().hex, uuid.uuid4().hex
    for host_id in (source, destination):
        hosts.upsert_on_connect(host_id, host_id, "alice")
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, source, workspace="/tmp/source")
    reached, resume, admission_started = Event(), Event(), Event()
    cleanup = accounts_module._revoke_durable_authority

    def pause_cleanup(*args, **kwargs):
        reached.set()
        assert resume.wait(10)
        return cleanup(*args, **kwargs)

    def admit_transfer():
        admission_started.set()
        hosts.admit_launch(
            destination,
            conv.id,
            "alice",
            account.account_generation,
            transfer_from_host_id=source,
        )

    monkeypatch.setattr(accounts_module, "_revoke_durable_authority", pause_cleanup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        deleting = pool.submit(accounts.delete_user, "alice")
        try:
            assert reached.wait(10)
            admitting = pool.submit(admit_transfer)
            assert admission_started.wait(10)
            with pytest.raises(TimeoutError):
                admitting.result(timeout=0.1)
        finally:
            resume.set()
        assert deleting.result(timeout=10) is True
        with pytest.raises(OmnigentError, match="revoked"):
            admitting.result(timeout=10)
    assert conversations.get_conversation(conv.id).runner_id is None


@pytest.mark.parametrize("revoked_user", ["actor", "target", "both"])
def test_target_scope_preserves_actor_and_target_checks(db_uri, revoked_user):
    from omnigent.db.account_authority import target_account_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    actor = accounts.create_user_with_password("z-actor", "actor-hash")
    target = accounts.create_user_with_password("a-target", "target-hash")
    with account_authority_scope(actor.id, actor.account_generation):
        with target_account_scope(target.id, target.account_generation):
            permissions.ensure_user(target.id)
    for user in ("target", "actor") if revoked_user == "both" else (revoked_user,):
        user_id = actor.id if user == "actor" else target.id
        assert accounts.delete_user(user_id) is True
        accounts.create_user_with_password(user_id, "replacement-hash")
    with account_authority_scope(actor.id, actor.account_generation):
        with target_account_scope(target.id, target.account_generation):
            with pytest.raises(OmnigentError, match="revoked") as caught:
                permissions.grant(target.id, uuid.uuid4().hex, 1)
            assert caught.value.code == (
                ErrorCode.CONFLICT if revoked_user == "target" else ErrorCode.UNAUTHORIZED
            )
    with account_authority_scope(actor.id, accounts.get_user(actor.id).account_generation):
        with target_account_scope(target.id, accounts.get_user(target.id).account_generation):
            permissions.ensure_user(target.id)
            assert (
                permissions.get_user(target.id).account_generation
                == accounts.get_user(target.id).account_generation
            )


@pytest.mark.parametrize("existing_external_user", [False, True])
def test_target_without_registration_cannot_acquire_new_account(db_uri, existing_external_user):
    from omnigent.db.account_authority import target_account_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    actor = accounts.create_user_with_password("actor", "actor-hash")
    if existing_external_user:
        permissions.ensure_user("target")
    target = permissions.get_user("target")
    with account_authority_scope(actor.id, actor.account_generation):
        with target_account_scope("target", target.account_generation if target else None):
            if existing_external_user:
                assert accounts.delete_user("target") is True
            accounts.create_user_with_password("target", "new-password-hash")
            with pytest.raises(OmnigentError, match="revoked"):
                permissions.ensure_user("target")
            with pytest.raises(OmnigentError, match="revoked"):
                permissions.grant("target", uuid.uuid4().hex, 1)


@pytest.mark.parametrize("change", ["host_owner", "session_owner", "host", "runner", "permission"])
def test_runner_issuance_rechecks_both_owners_and_binding(db_uri, monkeypatch, change):
    from omnigent.db import account_authority
    from omnigent.db.account_authority import account_generation
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    accounts = SqlAlchemyAccountStore(db_uri)
    owner = accounts.create_user_with_password("a-session-owner", "hash")
    host_owner = accounts.create_user_with_password("z-host-owner", "hash")
    hosts = HostStore(db_uri)
    host_id, other_host_id, runner_id = (uuid.uuid4().hex for _ in range(3))
    hosts.upsert_on_connect(host_id, "first", host_owner.id)
    hosts.upsert_on_connect(other_host_id, "other", host_owner.id)
    conversations = SqlAlchemyConversationStore(db_uri)
    conv = conversations.create_conversation(
        host_id=host_id, workspace="/tmp", runner_id=runner_id
    )
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant(owner.id, conv.id, LEVEL_OWNER)
    locked = []
    lock = account_authority.lock_account

    def record_lock(session, user_id):
        locked.append(user_id)
        return lock(session, user_id)

    monkeypatch.setattr(account_authority, "lock_account", record_lock)

    def issue(user_id):
        assert user_id == owner.id
        assert account_generation(user_id) == owner.account_generation
        return "issued"

    assert accounts.with_runner_authority(runner_id, issue) == "issued"
    assert locked == sorted([owner.id, host_owner.id])
    if accounts._engine.dialect.name == "sqlite":
        pytest.skip("SQLite already holds its database-wide writer lock at the snapshot")
    reached, resume = Event(), Event()
    check = accounts_module.require_active_account

    def pause_before_locks(*args, **kwargs):
        reached.set()
        assert resume.wait(15)
        return check(*args, **kwargs)

    # Pause after the initial binding snapshot but before any account lock.
    monkeypatch.setattr(accounts_module, "require_active_account", pause_before_locks)
    issued = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(accounts.with_runner_authority, runner_id, issued.append)
        try:
            assert reached.wait(10)
            monkeypatch.setattr(accounts_module, "require_active_account", check)
            if change in ("host_owner", "session_owner"):
                target = host_owner if change == "host_owner" else owner
                assert accounts.delete_user(target.id) is True
                accounts.create_user_with_password(target.id, "replacement-hash")
            elif change == "host":
                conversations.set_host_id(conv.id, other_host_id)
            elif change == "runner":
                conversations.replace_runner_id(conv.id, uuid.uuid4().hex)
            else:
                permissions.revoke(owner.id, conv.id)
        finally:
            resume.set()
        if change in ("host_owner", "session_owner"):
            with pytest.raises(OmnigentError) as caught:
                pending.result(timeout=15)
            assert caught.value.code == ErrorCode.UNAUTHORIZED
        else:
            assert pending.result(timeout=15) is None
    assert issued == []
