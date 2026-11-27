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
from omnigent.stores.inbox_item_store import NewInboxItem
from omnigent.stores.inbox_item_store.sqlalchemy_store import SqlAlchemyInboxItemStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.proactive_agent_settings_store import ProactiveAgentSettingsStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from omnigent.stores.workflow_store import WorkflowStore
from tests.gadgets.grant_store import GadgetGrantStore


def test_account_generation_survives_delete_and_rejects_old_writes(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    old = accounts.create_user_with_password("alice", "test-password-hash")
    workflows = WorkflowStore(db_uri)
    saved = workflows.create(
        user_id="alice", name="Private workflow", description="Private steps.", steps=[]
    )
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
            lambda: permissions.grant_if_absent("alice", uuid.uuid4().hex, 1),
            lambda: permissions.grant_for_scheduled_task("alice", uuid.uuid4().hex, 1),
            lambda: permissions.grant_for_workflow("alice", uuid.uuid4().hex, 1),
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
            lambda: workflows.create(
                user_id="alice", name="Late workflow", description="Late steps.", steps=[]
            ),
            lambda: workflows.update(saved.id, user_id="alice", name="Late edit"),
            lambda: workflows.delete(saved.id, user_id="alice"),
        ):
            with pytest.raises(OmnigentError, match="revoked"):
                write()
    with account_authority_scope("alice", new.account_generation):
        permissions.ensure_user("alice")
        assert HostStore(db_uri).upsert_on_connect(uuid.uuid4().hex, "laptop", "alice")
        assert workflows.list_page(user_id="alice", limit=10) == ([], False)
        assert workflows.get(saved.id, user_id="alice") is None
        assert workflows.update(saved.id, user_id="alice", name="Replacement edit") is None
        assert workflows.delete(saved.id, user_id="alice") is False


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


def test_session_revocation_moves_only_current_registration_work(db_uri: str) -> None:
    from sqlalchemy import select

    from omnigent.db.db_models import SqlHost

    accounts = SqlAlchemyAccountStore(db_uri)
    hosts = HostStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    grants = DeviceGrantStore(db_uri)

    def create_task():
        return tasks.create(
            uuid.uuid4().hex, "job", "hello", "FREQ=DAILY", "alice", uuid.uuid4().hex, "UTC"
        )

    def register_managed_host(name: str) -> str:
        host_id = uuid.uuid4().hex
        hosts.register_managed_host(
            host_id=host_id,
            name=name,
            user_id="alice",
            token=f"{name}-token",
            provider="modal",
            sandbox_id=f"{name}-sandbox",
            token_expires_at=int(time.time()) + 3600,
        )
        return host_id

    def stored_host_generation(host_id: str) -> str | None:
        # Hosts retained for sandbox cleanup are hidden from get_host.
        with accounts._engine.connect() as connection:
            return connection.execute(
                select(SqlHost.account_generation).where(SqlHost.host_id == host_id)
            ).scalar_one()

    first = accounts.create_user_with_password("alice", "first-hash").account_generation
    predecessor_task = create_task()
    predecessor_host_id = register_managed_host("predecessor")
    assert accounts.delete_user("alice") is True
    assert hosts.get_host(predecessor_host_id) is None
    assert stored_host_generation(predecessor_host_id) == first
    old = accounts.create_user_with_password("alice", "second-hash").account_generation
    workflows = WorkflowStore(db_uri)
    workflow = workflows.create(
        user_id="alice", name="Preserved workflow", description="Keep these steps.", steps=[]
    )
    task = create_task()
    host_id = register_managed_host("managed")
    grants.create_redeemed_grant(
        "cli-grant",
        user_id="alice",
        client_id="omnigent-cli",
        refresh_token_hash="hash",
        created_at=int(time.time()),
    )
    accounts.create_token(
        "magic",
        kind="magic",
        user_id="alice",
        created_by=None,
        created_at=0,
        expires_at=2000000000,
    )

    # The login-time rehash keeps the generation.
    assert accounts.update_password("alice", "rehashed") == old
    new = accounts.update_password("alice", "third-hash", revoke_sessions=True)
    assert new not in (None, old)
    assert not accounts.accepts_generation("alice", old)
    assert accounts.accepts_generation("alice", new)
    assert workflows.get(workflow.id, user_id="alice") == workflow
    assert tasks.get(task.id).account_generation == new
    assert tasks.get(predecessor_task.id).account_generation == first
    assert stored_host_generation(predecessor_host_id) == first
    assert hosts.get_host(host_id).account_generation == new
    assert hosts.replace_managed_host_sandbox(
        host_id=host_id,
        user_id="alice",
        token="replacement",
        provider="modal",
        sandbox_id="new-sandbox",
        token_expires_at=int(time.time()) + 3600,
    )
    with pytest.raises(OmnigentError, match="revoked"):
        grants.authorize_access("cli-grant")
    magic = accounts.redeem_token("magic", kind="magic", now_epoch_seconds=1)
    assert magic is not None and magic.account_generation == old


def test_failed_session_revocation_rolls_back_password_and_work(db_uri: str) -> None:
    import re

    import sqlalchemy as sa

    accounts = SqlAlchemyAccountStore(db_uri)
    hosts = HostStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    generation = accounts.create_user_with_password("alice", "old-hash").account_generation
    task = tasks.create(
        uuid.uuid4().hex, "job", "hello", "FREQ=DAILY", "alice", uuid.uuid4().hex, "UTC"
    )
    host = hosts.upsert_on_connect(uuid.uuid4().hex, "laptop", "alice")

    updated: list[str] = []

    def fail_host_update(_conn, _cursor, statement, *_args) -> None:
        match = re.search(r"\bUPDATE\s+(\w+)", statement)
        if match:
            updated.append(match.group(1))
            if match.group(1) == "hosts":
                raise RuntimeError("injected failure after the task update")

    sa.event.listen(accounts._engine, "before_cursor_execute", fail_host_update)
    try:
        with pytest.raises(RuntimeError, match="injected failure"):
            accounts.update_password("alice", "new-hash", revoke_sessions=True)
    finally:
        sa.event.remove(accounts._engine, "before_cursor_execute", fail_host_update)
    assert updated == ["scheduled_tasks", "hosts"]
    assert accounts.get_password_hash("alice") == "old-hash"
    assert accounts.get_user("alice").account_generation == generation
    assert accounts.accepts_generation("alice", generation)
    assert tasks.get(task.id).account_generation == generation
    assert hosts.get_host(host.host_id).account_generation == generation


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


def _create_task(db_uri: str, owner: str) -> str:
    return (
        SqlAlchemyScheduledTaskStore(db_uri)
        .create(uuid.uuid4().hex, "job", "hello", "FREQ=DAILY", owner, uuid.uuid4().hex, "UTC")
        .id
    )


def test_deletion_removes_automation_grants_held_and_owned(db_uri, monkeypatch):
    from omnigent.db.db_models import workspace_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    # One grant or task per batch, so each cleanup spans several transactions.
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    with workspace_scope(202):
        permissions.ensure_user("alice")
        elsewhere = _create_task(db_uri, "dave")
        permissions.grant_for_scheduled_task("alice", elsewhere, 1)
    accounts.create_user_with_password("alice", "alice-hash")
    for grantee in ("carol", "erin"):
        permissions.ensure_user(grantee)
    owned, owned_deleted, other, another = (
        _create_task(db_uri, owner) for owner in ("alice", "alice", "dave", "dave")
    )
    for task_id, grantee in (
        (owned, "carol"),
        (owned, "erin"),
        (owned_deleted, "carol"),
        (other, "alice"),
        (other, "carol"),
        (another, "alice"),
    ):
        permissions.grant_for_scheduled_task(grantee, task_id, 1)
    # Marking a task deleted leaves its grants in place.
    assert SqlAlchemyScheduledTaskStore(db_uri).update(owned_deleted, state="deleted")

    assert accounts.delete_user("alice") is True
    for task_id in (owned, owned_deleted, another):
        assert permissions.list_for_scheduled_task(task_id) == ([], None)
    assert [grant.user_id for grant in permissions.list_for_scheduled_task(other)[0]] == ["carol"]
    with pytest.raises(OmnigentError, match="revoked"):
        permissions.grant_for_scheduled_task("alice", other, 1)
    accounts.create_user_with_password("alice", "replacement-hash")
    assert permissions.resolve_scheduled_task_access(["alice"], other) is None
    with workspace_scope(202):
        assert permissions.resolve_scheduled_task_access(["alice"], elsewhere) == 1


def test_reregistration_finishes_an_interrupted_grant_cleanup(db_uri, monkeypatch):
    from omnigent.stores.permission_store import sqlalchemy_store as permission_module

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    accounts.create_user_with_password("bob", "bob-hash")
    task_ids = [_create_task(db_uri, "dave") for _ in range(3)]
    for task_id in task_ids:
        permissions.grant_for_scheduled_task("bob", task_id, 1)

    def held_grants() -> int:
        return sum(
            permissions.resolve_scheduled_task_access(["bob"], task_id) is not None
            for task_id in task_ids
        )

    delete_batch = permission_module._delete_grant_batch
    batches = 0

    def interrupted_after_one_batch(*args, **kwargs):
        nonlocal batches
        batches += 1
        if batches > 1:
            raise RuntimeError("test cleanup interruption")
        return delete_batch(*args, **kwargs)

    monkeypatch.setattr(permission_module, "_delete_grant_batch", interrupted_after_one_batch)
    assert accounts.delete_user("bob") is True
    assert held_grants() == 2
    monkeypatch.setattr(permission_module, "_delete_grant_batch", delete_batch)

    # A grant still held refuses the name; registration's own cleanup is skipped here.
    finish_cleanup = accounts_module.delete_deleted_account_grants
    monkeypatch.setattr(accounts_module, "delete_deleted_account_grants", lambda *a, **k: 0)
    with pytest.raises(OmnigentError) as caught:
        accounts.create_user_with_password("bob", "replacement-hash")
    assert caught.value.code == ErrorCode.CONFLICT
    assert accounts.get_user("bob") is None
    monkeypatch.setattr(accounts_module, "delete_deleted_account_grants", finish_cleanup)

    assert accounts.create_user_with_password("bob", "replacement-hash").id == "bob"
    assert held_grants() == 0


def test_owned_task_cleanup_spares_a_replacement_accounts_live_task(db_uri, monkeypatch):
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    workflows = WorkflowStore(db_uri)
    accounts.create_user_with_password("alice", "alice-hash")
    old_workflow = workflows.create(
        user_id="alice", name="Plan", description="Old account steps.", steps=[]
    )
    for grantee in ("bob", "carol"):
        permissions.ensure_user(grantee)
    old_task = _create_task(db_uri, "alice")
    permissions.grant_for_scheduled_task("carol", old_task, 1)
    reached, resume = Event(), Event()
    owned_task_keys = accounts._owned_task_keys

    def paused_before_owned_tasks(*args, **kwargs):
        reached.set()
        assert resume.wait(10)
        return owned_task_keys(*args, **kwargs)

    # Pause after the held grants are gone and before the owned-task pass.
    monkeypatch.setattr(accounts, "_owned_task_keys", paused_before_owned_tasks)
    with ThreadPoolExecutor(max_workers=1) as pool:
        deleting = pool.submit(accounts.delete_user, "alice")
        try:
            assert reached.wait(10)
            accounts.create_user_with_password("alice", "replacement-hash")
            new_task = _create_task(db_uri, "alice")
            permissions.grant_for_scheduled_task("bob", new_task, 1)
            new_workflow = workflows.create(
                user_id="alice", name="Plan", description="New account steps.", steps=[]
            )
        finally:
            resume.set()
        assert deleting.result(timeout=10) is True
    assert permissions.list_for_scheduled_task(old_task) == ([], None)
    assert [grant.user_id for grant in permissions.list_for_scheduled_task(new_task)[0]] == ["bob"]
    assert workflows.get(old_workflow.id, user_id="alice") is None
    assert workflows.get(new_workflow.id, user_id="alice") == new_workflow


def test_reregistration_finishes_an_interrupted_workflow_cleanup(db_uri, monkeypatch):
    import sqlalchemy as sa

    accounts = SqlAlchemyAccountStore(db_uri)
    workflows = WorkflowStore(db_uri)
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    accounts.create_user_with_password("alice", "alice-hash")
    saved = [
        workflows.create(user_id="alice", name=f"Plan {i}", description="Private steps.", steps=[])
        for i in range(3)
    ]
    deletes = 0

    def interrupt_cleanup(_connection, _cursor, _statement, parameters, context, _executemany):
        nonlocal deletes
        query = getattr(context.compiled, "statement", None)
        if isinstance(query, sa.Delete) and query.table.name == "workflows":
            # The database fails after one committed batch; retries must retain the tombstone.
            assert len(parameters) == 2  # workspace and one workflow ID
            deletes += 1
            if deletes > 1:
                raise RuntimeError("test workflow cleanup interruption")

    sa.event.listen(accounts._engine, "before_cursor_execute", interrupt_cleanup)
    try:
        assert accounts.delete_user("alice") is True
        assert sum(workflows.get(item.id, user_id="alice") is not None for item in saved) == 2
        with pytest.raises(RuntimeError, match="test workflow cleanup interruption"):
            accounts.create_user_with_password("alice", "replacement-hash")
        assert accounts.get_user("alice") is None
    finally:
        sa.event.remove(accounts._engine, "before_cursor_execute", interrupt_cleanup)
    assert accounts.create_user_with_password("alice", "replacement-hash").id == "alice"
    assert workflows.list_page(user_id="alice", limit=10) == ([], False)


def _create_workflow(db_uri: str, owner: str) -> str:
    return (
        WorkflowStore(db_uri)
        .create(user_id=owner, name=f"Plan {uuid.uuid4().hex[:8]}", description="Steps.", steps=[])
        .id
    )


def test_deletion_removes_workflow_grants_held_and_on_owned_workflows(db_uri, monkeypatch):
    from omnigent.db.db_models import workspace_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    workflows = WorkflowStore(db_uri)
    # One grant or workflow per batch, so each cleanup spans several transactions.
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    with workspace_scope(202):
        permissions.ensure_user("alice")
        elsewhere = _create_workflow(db_uri, "dave")
        permissions.grant_for_workflow("alice", elsewhere, 1)
    accounts.create_user_with_password("alice", "alice-hash")
    for grantee in ("carol", "erin"):
        permissions.ensure_user(grantee)
    owned, owned_too, other, another = (
        _create_workflow(db_uri, owner) for owner in ("alice", "alice", "dave", "dave")
    )
    for workflow_id, grantee in (
        (owned, "carol"),
        (owned, "erin"),
        (owned_too, "carol"),
        (other, "alice"),
        (other, "carol"),
        (another, "alice"),
    ):
        permissions.grant_for_workflow(grantee, workflow_id, 1)

    assert accounts.delete_user("alice") is True
    for workflow_id in (owned, owned_too, another):
        assert permissions.list_for_workflow(workflow_id) == ([], None)
    assert workflows.get(owned, user_id="alice") is None
    assert [grant.user_id for grant in permissions.list_for_workflow(other)[0]] == ["carol"]
    with pytest.raises(OmnigentError, match="revoked"):
        permissions.grant_for_workflow("alice", other, 1)
    accounts.create_user_with_password("alice", "replacement-hash")
    assert permissions.resolve_workflow_access(["alice"], other) is None
    with workspace_scope(202):
        assert permissions.resolve_workflow_access(["alice"], elsewhere) == 1


def test_owned_workflows_lose_their_grants_a_bounded_batch_at_a_time(db_uri, monkeypatch):
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 2)
    accounts.create_user_with_password("alice", "alice-hash")
    owned = [_create_workflow(db_uri, "alice") for _ in range(3)]
    for grantee in ("bob", "carol", "erin"):
        permissions.ensure_user(grantee)
        for workflow_id in owned:
            permissions.grant_for_workflow(grantee, workflow_id, 1)
    batches: list[int] = []
    delete_grants = accounts_module.delete_grants_on_workflows

    def recording(session, workflow_ids, *, batch_size):
        batches.append(delete_grants(session, workflow_ids, batch_size=batch_size))
        return batches[-1]

    monkeypatch.setattr(accounts_module, "delete_grants_on_workflows", recording)
    assert accounts.delete_user("alice") is True
    assert sum(batches) == 9
    assert max(batches) <= 2
    for workflow_id in owned:
        assert permissions.list_for_workflow(workflow_id) == ([], None)
        assert WorkflowStore(db_uri).get(workflow_id, user_id="alice") is None


def test_reregistration_finishes_an_interrupted_workflow_grant_cleanup(db_uri, monkeypatch):
    from omnigent.db.db_models import SqlWorkflowShare
    from omnigent.stores.permission_store import sqlalchemy_store as permission_module

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    accounts.create_user_with_password("bob", "bob-hash")
    workflow_ids = [_create_workflow(db_uri, "dave") for _ in range(3)]
    for workflow_id in workflow_ids:
        permissions.grant_for_workflow("bob", workflow_id, 1)

    def held_grants() -> int:
        return sum(
            permissions.resolve_workflow_access(["bob"], workflow_id) is not None
            for workflow_id in workflow_ids
        )

    delete_batch = permission_module._delete_grant_batch
    batches = 0

    def interrupted_after_one_batch(*args, **kwargs):
        nonlocal batches
        if kwargs["model"] is SqlWorkflowShare:
            batches += 1
            if batches > 1:
                raise RuntimeError("test cleanup interruption")
        return delete_batch(*args, **kwargs)

    monkeypatch.setattr(permission_module, "_delete_grant_batch", interrupted_after_one_batch)
    assert accounts.delete_user("bob") is True
    assert held_grants() == 2
    monkeypatch.setattr(permission_module, "_delete_grant_batch", delete_batch)

    # A grant still held refuses the name; registration's own cleanup is skipped here.
    finish_cleanup = accounts_module.delete_deleted_account_workflow_grants
    monkeypatch.setattr(
        accounts_module, "delete_deleted_account_workflow_grants", lambda *a, **k: 0
    )
    with pytest.raises(OmnigentError, match="workflow grants") as caught:
        accounts.create_user_with_password("bob", "replacement-hash")
    assert caught.value.code == ErrorCode.CONFLICT
    assert accounts.get_user("bob") is None
    monkeypatch.setattr(accounts_module, "delete_deleted_account_workflow_grants", finish_cleanup)

    assert accounts.create_user_with_password("bob", "replacement-hash").id == "bob"
    assert held_grants() == 0


def test_deleted_accounts_names_only_tombstoned_users(db_uri):
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    accounts.create_user_with_password("alice", "alice-hash")
    accounts.create_user_with_password("bob", "bob-hash")
    assert accounts.delete_user("alice") is True
    # "carol" has no account row, as an identity an SSO proxy supplies may not.
    assert permissions.deleted_accounts(["alice", "bob", "carol", "alice"]) == {"alice"}
    assert permissions.deleted_accounts([]) == set()
    # MySQL's key folds case and accents, so a spelling it equates is deleted too,
    # and comes back as given, which is how a workflow may store its owner.
    variants = ["ALICE", "alíce"]
    expected = set(variants) if permissions._engine.dialect.name == "mysql" else set()
    assert permissions.deleted_accounts([*variants, "bob"]) == expected
    accounts.create_user_with_password("alice", "replacement-hash")
    assert permissions.deleted_accounts(["alice"]) == set()


def test_deletion_removes_all_preferences_only_for_user_in_workspace(db_uri: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from omnigent.db.db_models import SqlPreference, workspace_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    workflows = WorkflowStore(db_uri)
    saved = {}
    for workspace_id in (101, 102):
        with workspace_scope(workspace_id):
            for user_id in ("alice", "bob"):
                accounts.create_user_with_password(user_id, "test-password-hash")
                saved[workspace_id, user_id] = workflows.create(
                    user_id=user_id,
                    name="Private workflow",
                    description="Private steps.",
                    steps=[],
                )
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
    for (workspace_id, user_id), workflow in saved.items():
        with workspace_scope(workspace_id):
            expected = None if (workspace_id, user_id) == (101, "alice") else workflow
            assert workflows.get(workflow.id, user_id=user_id) == expected


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
    permissions.ensure_user("carol")
    owned_task, other_task = _create_task(db_uri, "alice"), _create_task(db_uri, "dave")
    permissions.grant_for_scheduled_task("carol", owned_task, 1)
    permissions.grant_for_scheduled_task("alice", other_task, 1)
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
    # The task tombstone rolls back, and grant cleanup only follows a committed deletion.
    assert SqlAlchemyScheduledTaskStore(db_uri).get(owned_task).state == "active"
    assert permissions.resolve_scheduled_task_access(["carol"], owned_task) == 1
    assert permissions.resolve_scheduled_task_access(["alice"], other_task) == 1


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
                    ],
                    approved=True,
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


@pytest.mark.parametrize("delete_first", [False, True])
def test_automation_grant_serializes_with_owner_deletion(db_uri, monkeypatch, delete_first):
    from omnigent.stores.permission_store import sqlalchemy_store as permission_module

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("carol")
    task_id = _create_task(db_uri, "alice")
    permissions.grant_for_scheduled_task("carol", task_id, 1)
    reached, resume, second_started = Event(), Event(), Event()
    # Pause each writer once it holds the task row: deletion after it tombstones the
    # task, the grant before it writes the grant row the cleanup after deletion removes.
    module, name = (
        (accounts_module, "_revoke_durable_authority")
        if delete_first
        else (permission_module, "_lock_live_scheduled_task")
    )
    original = getattr(module, name)

    def paused_lock(*args, **kwargs):
        result = original(*args, **kwargs)
        reached.set()
        assert resume.wait(10)
        return result

    monkeypatch.setattr(module, name, paused_lock)

    def grant():
        return permissions.grant_for_scheduled_task("carol", task_id, 1)

    def second():
        second_started.set()
        return grant() if delete_first else accounts.delete_user("alice")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(accounts.delete_user, "alice") if delete_first else pool.submit(grant)
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
            with pytest.raises(OmnigentError) as caught:
                waiting.result(timeout=10)
            assert caught.value.code == ErrorCode.NOT_FOUND
        else:
            assert waiting.result(timeout=10) is True
    assert permissions.list_for_scheduled_task(task_id) == ([], None)


def test_owner_grant_and_pause_lock_the_account_before_the_task(db_uri, monkeypatch):
    from omnigent.stores.permission_store import sqlalchemy_store as permission_module

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    alice = accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("carol")
    task_id = _create_task(db_uri, "alice")
    holds_account, resume, pause_started = Event(), Event(), Event()
    original = permission_module._lock_live_scheduled_task

    def paused_before_task_lock(*args, **kwargs):
        holds_account.set()
        assert resume.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(permission_module, "_lock_live_scheduled_task", paused_before_task_lock)

    def as_alice(write):
        with account_authority_scope("alice", alice.account_generation):
            return write()

    def pause():
        pause_started.set()
        return as_alice(lambda: tasks.update(task_id, state="paused"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        granting = pool.submit(
            as_alice, lambda: permissions.grant_for_scheduled_task("carol", task_id, 1)
        )
        try:
            assert holds_account.wait(10)
            pausing = pool.submit(pause)
            assert pause_started.wait(10)
            # Long enough for a pause that took the task row first to reach the account.
            with pytest.raises(TimeoutError):
                pausing.result(timeout=0.5)
        finally:
            resume.set()
        assert granting.result(timeout=10).user_id == "carol"
        assert pausing.result(timeout=10).state == "paused"


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
            for grant in (permissions.grant, permissions.grant_for_scheduled_task):
                with pytest.raises(OmnigentError, match="revoked") as caught:
                    grant(target.id, uuid.uuid4().hex, 1)
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
            for write in (
                lambda: permissions.ensure_user("target"),
                lambda: permissions.grant("target", uuid.uuid4().hex, 1),
                lambda: permissions.grant_if_absent("target", uuid.uuid4().hex, 1),
                lambda: permissions.grant_for_scheduled_task("target", uuid.uuid4().hex, 1),
            ):
                with pytest.raises(OmnigentError, match="revoked"):
                    write()


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


def _inbox_item(user_id: str, dedupe_key: str = "v1:private") -> NewInboxItem:
    return NewInboxItem(
        user_id=user_id,
        source_session_id=uuid.uuid4().hex,
        agent_id=uuid.uuid4().hex,
        kind="info",
        title="A private suggestion",
        body_md="Only the account that got it may read it.",
        links=[],
        actions=[],
        media=[],
        primary_link=None,
        topic="private",
        dedupe_key=dedupe_key,
    )


def test_deleted_account_inbox_is_not_visible_after_reregistration(db_uri: str) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    inbox = SqlAlchemyInboxItemStore(db_uri)
    old = accounts.create_user_with_password("alice", "test-password-hash")
    with account_authority_scope("alice", old.account_generation):
        item = inbox.create(_inbox_item("alice"))
    assert accounts.delete_user("alice") is True
    new = accounts.create_user_with_password("alice", "replacement-password-hash")

    with account_authority_scope("alice", new.account_generation):
        assert inbox.list_for_user("alice") == ([], None)
        assert inbox.get(item.id, "alice") is None
        assert inbox.get_by_dedupe_key("alice", "v1:private") is None
    with account_authority_scope("alice", old.account_generation):
        for write in (
            lambda: inbox.create(_inbox_item("alice")),
            lambda: inbox.update(item.id, "alice", status="read", now=1),
        ):
            with pytest.raises(OmnigentError, match="revoked"):
                write()


def test_inbox_cleanup_is_batched_and_finished_before_reregistration(db_uri, monkeypatch):
    import sqlalchemy as sa

    from omnigent.db.db_models import SqlInboxItem, workspace_scope

    accounts = SqlAlchemyAccountStore(db_uri)
    inbox = SqlAlchemyInboxItemStore(db_uri)
    monkeypatch.setattr(accounts, "_cleanup_batch_size", 1)
    old = accounts.create_user_with_password("alice", "alice-hash")
    with account_authority_scope("alice", old.account_generation):
        # One item from each of three sessions, as the per-session cap does not bound them.
        for n in range(3):
            inbox.create(_inbox_item("alice", dedupe_key=f"v1:private-{n}"))

    def stored() -> int:
        with workspace_scope(None), accounts._engine.connect() as conn:
            return conn.execute(
                sa.select(sa.func.count()).where(SqlInboxItem.user_id == "alice")
            ).scalar_one()

    deletes = 0

    def interrupt_cleanup(_connection, _cursor, _statement, parameters, context, _executemany):
        nonlocal deletes
        query = getattr(context.compiled, "statement", None)
        if isinstance(query, sa.Delete) and query.table.name == "inbox_items":
            # Each committed batch deletes one row: the workspace and one item ID.
            assert len(parameters) == 2
            deletes += 1
            if deletes > 1:
                raise RuntimeError("test inbox cleanup interruption")

    sa.event.listen(accounts._engine, "before_cursor_execute", interrupt_cleanup)
    try:
        # The tombstone commits first; the failed cleanup is deferred, not the deletion.
        assert accounts.delete_user("alice") is True
        assert stored() == 2
        with pytest.raises(RuntimeError, match="test inbox cleanup interruption"):
            accounts.create_user_with_password("alice", "replacement-hash")
        assert accounts.get_user("alice") is None
    finally:
        sa.event.remove(accounts._engine, "before_cursor_execute", interrupt_cleanup)

    new = accounts.create_user_with_password("alice", "replacement-hash")
    assert stored() == 0
    with account_authority_scope("alice", new.account_generation):
        fresh = inbox.create(_inbox_item("alice", dedupe_key="v1:fresh"))
    # Cleanup that runs late sees a live account and leaves its items alone.
    accounts._delete_deleted_inbox_items("alice")
    with account_authority_scope("alice", new.account_generation):
        assert [item.id for item in inbox.list_for_user("alice")[0]] == [fresh.id]


def test_workspace_setting_write_rechecks_the_admin_under_its_lock(db_uri: str) -> None:
    """A deleted or demoted admin's in-flight workspace write cannot land."""
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    settings = ProactiveAgentSettingsStore(db_uri)
    # A second admin, so deleting the first one below is allowed.
    accounts.create_user_with_password("root", "test-password-hash")
    permissions.set_admin("root", True)
    admin = accounts.create_user_with_password("admin", "test-password-hash")
    permissions.set_admin("admin", True)
    with account_authority_scope("admin", admin.account_generation):
        settings.set_workspace_enabled(False, actor="admin", require_admin=True)
    assert settings.read("alice") == (False, True)

    # The route authorized the request while the actor was still an admin.
    permissions.set_admin("admin", False)
    with account_authority_scope("admin", admin.account_generation):
        with pytest.raises(OmnigentError) as demoted:
            settings.set_workspace_enabled(True, actor="admin", require_admin=True)
    assert demoted.value.code == ErrorCode.FORBIDDEN
    assert settings.read("alice") == (False, True)

    permissions.set_admin("admin", True)
    assert accounts.delete_user("admin") is True
    with account_authority_scope("admin", admin.account_generation):
        with pytest.raises(OmnigentError, match="revoked"):
            settings.set_workspace_enabled(True, actor="admin", require_admin=True)
    assert settings.read("alice") == (False, True)


def test_prompt_write_rechecks_the_admin_under_its_lock(db_uri: str) -> None:
    """A deleted or demoted admin's in-flight prompt write cannot land."""
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    settings = ProactiveAgentSettingsStore(db_uri)
    # A second admin, so deleting the first one below is allowed.
    accounts.create_user_with_password("root", "test-password-hash")
    permissions.set_admin("root", True)
    admin = accounts.create_user_with_password("admin", "test-password-hash")
    permissions.set_admin("admin", True)
    with account_authority_scope("admin", admin.account_generation):
        settings.set_prompt_override(
            "Look for stale pull requests.", actor="admin", require_admin=True
        )
    assert settings.read_prompt_override() == "Look for stale pull requests."

    # The route authorized the request while the actor was still an admin.
    permissions.set_admin("admin", False)
    with account_authority_scope("admin", admin.account_generation):
        with pytest.raises(OmnigentError) as demoted:
            settings.set_prompt_override("Post about anything.", actor="admin", require_admin=True)
    assert demoted.value.code == ErrorCode.FORBIDDEN
    assert settings.read_prompt_override() == "Look for stale pull requests."

    permissions.set_admin("admin", True)
    assert accounts.delete_user("admin") is True
    with account_authority_scope("admin", admin.account_generation):
        with pytest.raises(OmnigentError, match="revoked"):
            settings.set_prompt_override(None, actor="admin", require_admin=True)
    assert settings.read_prompt_override() == "Look for stale pull requests."


def _gadgets(db_uri: str, tmp_path, accounts: SqlAlchemyAccountStore):
    from omnigent.gadgets.access import PermissionStoreGadgetAccess
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.gadgets.service import GadgetService, GadgetViewer
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    permissions = GadgetGrantStore(db_uri)
    access = PermissionStoreGadgetAccess(store, permissions)
    accounts.attach_gadgets(DeletedAccountGadgets(store, access))

    class AsAccount:
        """A user's gadget service, each call made as a request of their account would be."""

        def __init__(self, user: str) -> None:
            account = accounts.get_user(user)
            self.generation = account.account_generation if account is not None else None
            self.user = user
            # As the gadget routes resolve a request's viewer.
            self.registration = permissions.account_registrations([user]).get(user)
            viewer = GadgetViewer(user, self.registration)
            self.service = GadgetService(store, access, viewer)

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            method = getattr(self.service, name)

            def call(*args, **kwargs):  # type: ignore[no-untyped-def]
                with account_authority_scope(self.user, self.generation):
                    return method(*args, **kwargs)

            return call

    return store, access, AsAccount


def test_deletion_removes_the_accounts_gadgets_and_who_they_were_shared_with(
    db_uri: str, tmp_path
) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    alice = service("alice")
    board = alice.create("Synthetic board")
    kept = service("bob").create("Bob's board")
    permissions.grant_for_gadget("bob", board["id"], 2)
    alice.pin(board["id"], {"pinned": True})

    assert accounts.delete_user("alice") is True
    assert list(store.iter_owned("alice", owner_registration=alice.registration)) == []
    assert store.get(board["id"]) is None
    assert permissions.list_for_gadget(board["id"]) == ([], None)
    assert access.shared_with("bob").items == []

    # The name starts over: no gadgets or pins come back with it.
    accounts.create_user_with_password("alice", "replacement-hash")
    assert service("alice").list_owned() == {"items": [], "next_cursor": None}
    assert service("alice").shelf()["pinned"] == []
    assert store.get(kept["id"]) is not None


def test_a_write_on_its_way_when_the_account_is_deleted_is_cleaned_up_after_it_lands(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    """A create and a pin that had passed their checks before the account was deleted land
    after its cleanup finished; they queue the cleanup again, so nothing of the deleted
    registration stays behind, and the name registered again starts empty."""
    import threading

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    kept = service("bob").create("Bob's board")
    permissions.grant_for_gadget("alice", kept["id"], 1)
    alice = service("alice")
    registration = alice.registration
    assert registration is not None

    release = threading.Event()
    paused = threading.Barrier(3)
    real_create = store.create
    real_user_files = store.user_files

    def late_create(owner_id, name, **kwargs):  # type: ignore[no-untyped-def]
        if owner_id == "alice":
            paused.wait(timeout=10)
            release.wait(timeout=10)
        return real_create(owner_id, name, **kwargs)

    class LateFiles:
        waited = False

        def __init__(self, files) -> None:  # type: ignore[no-untyped-def]
            self._files = files

        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(self._files, name)

        def _pause(self) -> None:
            # Only the pin's first write waits; the shelf is reopened on every call.
            if not LateFiles.waited:
                LateFiles.waited = True
                paused.wait(timeout=10)
                release.wait(timeout=10)

        def mkdirs(self, path: str) -> None:
            # Paused before the pin's first write, which recreates the deleted scope.
            self._pause()
            self._files.mkdirs(path)

        def create_json(self, path: str, value: dict) -> None:
            self._pause()
            self._files.create_json(path, value)

    def user_files(user_id, **kwargs):  # type: ignore[no-untyped-def]
        files = real_user_files(user_id, **kwargs)
        if user_id == "alice" and kwargs.get("owner_registration") == registration:
            return LateFiles(files)
        return files

    monkeypatch.setattr(store, "create", late_create)
    monkeypatch.setattr(store, "user_files", user_files)
    errors: list[BaseException] = []

    def run(call) -> None:  # type: ignore[no-untyped-def]
        try:
            call()
        except BaseException as error:
            errors.append(error)

    writers = [
        threading.Thread(target=run, args=(lambda: alice.create("Late board"),)),
        threading.Thread(target=run, args=(lambda: alice.pin(kept["id"], {"pinned": True}),)),
    ]
    for writer in writers:
        writer.start()
    paused.wait(timeout=10)
    # Both writes are past their checks; the account goes, and its cleanup finishes.
    assert accounts.delete_user("alice") is True
    release.set()
    for writer in writers:
        writer.join(timeout=10)
    assert errors == []
    # They landed after the cleanup, for the deleted registration.
    assert len(list(store.iter_owned("alice", owner_registration=registration))) == 1
    assert (store._user_dir("alice", registration)).exists()

    monkeypatch.setattr(store, "user_files", real_user_files)
    for _ in range(3):
        accounts._gadgets.resume()
    assert list(store.iter_owned("alice", owner_registration=registration)) == []
    assert not store._user_dir("alice", registration).exists()
    accounts.create_user_with_password("alice", "replacement-hash")
    assert service("alice").list_owned() == {"items": [], "next_cursor": None}
    assert service("alice").shelf()["pinned"] == []
    assert store.get(kept["id"]) is not None


def _reopened_cleanup(db_uri: str, tmp_path):
    """The store and cleanup a restarted server would have, on the same files."""
    from omnigent.gadgets.access import PermissionStoreGadgetAccess
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    access = PermissionStoreGadgetAccess(store, GadgetGrantStore(db_uri))
    return store, DeletedAccountGadgets(store, access)


def test_a_write_landing_after_the_cleanups_last_look_is_cleaned_up_too(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    """A create lands after the cleanup found nothing left but before it said so; the
    cleanup must look again rather than finish over it."""
    from omnigent.gadgets import accounts as cleanup_module

    accounts = SqlAlchemyAccountStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    alice = service("alice")
    registration = alice.registration
    alice.create("Board")
    real_done = cleanup_module._CleanupQueue.done
    landed: list[str] = []

    def done_after_a_late_write(queue, number, name):  # type: ignore[no-untyped-def]
        if not landed:
            gadget = store.create("alice", "Late board", owner_registration=registration)
            landed.append(gadget.id)
            cleanup_module.DeletedAccountGadgets.after_registration_write(
                store, "alice", registration
            )
        return real_done(queue, number, name)

    monkeypatch.setattr(cleanup_module._CleanupQueue, "done", done_after_a_late_write)
    assert accounts.delete_user("alice") is True
    monkeypatch.setattr(cleanup_module._CleanupQueue, "done", real_done)
    assert store.get(landed[0]) is not None

    store, cleanup = _reopened_cleanup(db_uri, tmp_path)
    for _ in range(3):
        cleanup.resume()
    assert store.get(landed[0]) is None
    assert list(store.iter_owned("alice", owner_registration=registration)) == []


def test_a_key_left_without_its_note_never_keeps_a_later_cleanup_from_queueing(
    db_uri: str, tmp_path
) -> None:
    """A stop between removing a finished note and its key leaves the key alone; a write
    that lands later for the deleted registration still gets its cleanup queued."""
    from omnigent.gadgets import accounts as cleanup_module

    accounts = SqlAlchemyAccountStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    alice = service("alice")
    registration = alice.registration
    alice.create("Board")
    assert accounts.delete_user("alice") is True
    scope = store._user_dir(cleanup_module._CLEANUP_SCOPE, cleanup_module._CLEANUP_REGISTRATION)
    key = cleanup_module._key("alice", registration)
    # The state such a stop leaves: the key, naming a segment, without its note.
    (scope / "keys").mkdir(parents=True, exist_ok=True)
    (scope / "keys" / f"{key}.json").write_text('{"segment": 0}')
    assert not (scope / "queue" / "s00000000" / f"{key}.json").exists()

    late = store.create("alice", "Late board", owner_registration=registration)
    cleanup_module.DeletedAccountGadgets.after_registration_write(store, "alice", registration)

    store, cleanup = _reopened_cleanup(db_uri, tmp_path)
    for _ in range(3):
        cleanup.resume()
    assert store.get(late.id) is None


def test_a_gadget_whose_create_stopped_before_its_owner_marker_leaves_nothing_behind(
    db_uri: str, tmp_path
) -> None:
    import os

    from omnigent.stores.gadget_store.local import LocalGadgetStore

    accounts = SqlAlchemyAccountStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    alice = service("alice")
    stopped = alice.create("Half made")
    # The create stopped after its metadata, before its owner marker and its note's
    # removal; owner pages, and so account cleanup, never see it.
    location = tmp_path / "gadgets"
    [marker] = (location / "owners").rglob(stopped["id"])
    marker.unlink()
    note = location / ".creating" / stopped["id"]
    note.touch()
    os.utime(note, (1_000_000_000, 1_000_000_000))

    LocalGadgetStore(str(location))  # A restart finishes the interrupted create.
    assert accounts.delete_user("alice") is True
    assert store.get(stopped["id"]) is None
    assert list(store.iter_owned("alice", owner_registration=alice.registration)) == []


def test_a_password_change_keeps_the_accounts_gadgets_and_pins(db_uri: str, tmp_path) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    _, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    before = service("alice")
    board = before.create("Synthetic board")
    before.pin(board["id"], {"pinned": True})
    permissions.grant_for_gadget("bob", board["id"], 1)

    # Rotating the generation revokes Alice's sessions, not her gadgets.
    for round_ in range(2):
        accounts.update_password("alice", f"hash-{round_}", revoke_sessions=True)
    after = service("alice")
    assert after.generation != before.generation
    assert after.registration == before.registration
    assert [item["id"] for item in after.list_owned()["items"]] == [board["id"]]
    assert [pin["id"] for pin in after.shelf()["pinned"]] == [board["id"]]
    assert after.get(board["id"])["id"] == board["id"]
    assert [item.gadget.id for item in access.shared_with("bob").items] == [board["id"]]


@pytest.mark.parametrize("failure", ["corrupt", "unwritable"])
def test_a_password_change_needs_nothing_from_the_gadget_store(
    db_uri: str, tmp_path, monkeypatch, failure: str
) -> None:
    from omnigent.stores.gadget_store import CorruptData

    accounts = SqlAlchemyAccountStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    before = service("alice")
    board = before.create("Synthetic board")
    before.pin(board["id"], {"pinned": True})

    error = CorruptData("Invalid file") if failure == "corrupt" else PermissionError("read-only")

    def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise error

    # The gadget store is broken while the password changes: it is never asked.
    for name in ("user_files", "create", "get", "iter_owned", "delete_user_files"):
        monkeypatch.setattr(store, name, broken)
    generation = accounts.update_password("alice", "hash-1", revoke_sessions=True)
    assert generation is not None and generation != before.generation
    assert not accounts.accepts_generation("alice", before.generation)
    monkeypatch.undo()

    after = service("alice")
    assert [item["id"] for item in after.list_owned()["items"]] == [board["id"]]
    assert [pin["id"] for pin in after.shelf()["pinned"]] == [board["id"]]


def test_with_gadgets_off_deleting_an_account_only_notes_its_gadgets_cleanup(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    from omnigent.gadgets.access import PermissionStoreGadgetAccess
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    accounts = SqlAlchemyAccountStore(db_uri)
    store = LocalGadgetStore(str(tmp_path / "gadgets"), maintain=False)
    access = PermissionStoreGadgetAccess(store, GadgetGrantStore(db_uri))
    accounts.attach_gadgets(DeletedAccountGadgets(store, access, enabled=lambda: False))

    def untouched(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the gadget store's gadgets were used")

    for name in ("list_owned_page", "iter_owned", "delete", "delete_user_files", "get"):
        monkeypatch.setattr(store, name, untouched)
    accounts.create_user_with_password("alice", "alice-hash")
    accounts.update_password("alice", "hash-1", revoke_sessions=True)
    assert accounts.delete_user("alice") is True
    assert accounts.create_user_with_password("alice", "replacement-hash").id == "alice"
    # Only the cleanup note (and the lock its queue is written under) was written: no gadgets
    # were listed or deleted, and opening the store with gadgets off ran no cleanup of its own.
    root = tmp_path / "gadgets"
    written = {path.relative_to(root).parts[0] for path in root.rglob("*") if path.is_file()}
    assert written == {"users", ".locks"}


def test_a_cleanup_noted_while_gadgets_were_off_finishes_once_they_are_on(
    db_uri: str, tmp_path
) -> None:
    from omnigent.gadgets.accounts import DeletedAccountGadgets

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    gadgets_on = {"value": True}
    cleanup = DeletedAccountGadgets(store, access, enabled=lambda: gadgets_on["value"])
    accounts.attach_gadgets(cleanup)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    old_board = service("alice").create("Old board")
    service("alice").pin(old_board["id"], {"pinned": True})
    permissions.grant_for_gadget("bob", old_board["id"], 1)

    # Off: the deletion and a registration of the name again delete nothing.
    gadgets_on["value"] = False
    assert accounts.delete_user("alice") is True
    accounts.create_user_with_password("alice", "replacement-hash")
    assert store.get(old_board["id"]) is not None
    # The new account owns nothing of the old one's, even before the cleanup runs.
    assert service("alice").list_owned() == {"items": [], "next_cursor": None}
    gadgets_on["value"] = True
    new_board = service("alice").create("New board")

    # On again: the next cleanup call (a server start, another deletion or registration)
    # finishes the old registration's, and leaves the new account's alone.
    cleanup.resume()
    assert store.get(old_board["id"]) is None
    assert permissions.list_for_gadget(old_board["id"]) == ([], None)
    assert [item["id"] for item in service("alice").list_owned()["items"]] == [new_board["id"]]
    assert service("alice").shelf()["pinned"] == []


def test_deleting_an_account_after_password_changes_removes_its_gadgets_for_good(
    db_uri: str, tmp_path
) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    board = service("alice").create("Synthetic board")
    permissions.grant_for_gadget("bob", board["id"], 1)
    accounts.update_password("alice", "hash-1", revoke_sessions=True)
    accounts.update_password("alice", "hash-2", revoke_sessions=True)

    assert accounts.delete_user("alice") is True
    assert store.get(board["id"]) is None
    assert permissions.list_for_gadget(board["id"]) == ([], None)
    accounts.create_user_with_password("alice", "replacement-hash")
    # Bob's earlier access doesn't come back with the name, and the new Alice starts empty.
    assert access.shared_with("bob").items == []
    assert service("alice").list_owned() == {"items": [], "next_cursor": None}
    assert service("alice").shelf()["pinned"] == []


def test_a_name_registered_again_before_the_gadget_cleanup_inherits_nothing(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    old_alice = service("alice")
    board = old_alice.create("Synthetic board")
    permissions.grant_for_gadget("bob", board["id"], 1)
    original = store.delete
    # The gadget store can't delete while the account is deleted and registered again.
    monkeypatch.setattr(store, "delete", lambda gadget_id: False)
    assert accounts.delete_user("alice") is True
    assert accounts.create_user_with_password("alice", "replacement-hash").id == "alice"

    # The old gadget is still stored, but neither the new Alice nor Bob reaches it.
    assert store.get(board["id"]) is not None
    alice = service("alice")
    assert alice.list_owned() == {"items": [], "next_cursor": None}
    assert access.permissions("alice", store.get(board["id"])).read is False
    assert access.permissions("bob", store.get(board["id"])).read is False
    assert access.shared_with("bob").items == []

    # A later cleanup removes it, and never the new account's gadgets.
    mine = alice.create("New board")
    assert alice.registration != old_alice.registration
    monkeypatch.setattr(store, "delete", original)
    accounts._gadgets.delete_owned("alice", old_alice.registration)
    assert store.get(board["id"]) is None
    assert store.get(mine["id"]) is not None


def test_a_cleanup_that_resumes_after_reregistration_leaves_the_new_accounts_gadgets(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    accounts = SqlAlchemyAccountStore(db_uri)
    store, _access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    old_alice = service("alice")
    board = old_alice.create("Synthetic board")
    old_alice.pin(board["id"], {"pinned": True})
    gadgets = accounts._gadgets
    paused: list[tuple] = []
    original = type(gadgets).delete_owned
    # The first cleanup read the tombstone, then stopped before deleting anything.
    monkeypatch.setattr(
        type(gadgets), "delete_owned", lambda self, *args: paused.append((self, *args))
    )
    assert accounts.delete_user("alice") is True
    monkeypatch.setattr(type(gadgets), "delete_owned", original)
    # Another cleanup finishes, and the name is registered again and used.
    accounts._delete_deleted_gadgets("alice")
    assert accounts.create_user_with_password("alice", "replacement-hash").id == "alice"
    new_alice = service("alice")
    mine = new_alice.create("New board")
    new_alice.pin(mine["id"], {"pinned": True})

    # The first cleanup resumes with what it read: the deleted account's generation.
    assert len(paused) == 1
    original(*paused[0])

    assert store.get(mine["id"]) is not None
    assert [pin["id"] for pin in new_alice.shelf()["pinned"]] == [mine["id"]]
    assert new_alice.list_owned()["items"][0]["id"] == mine["id"]


def test_a_username_registered_again_cannot_reach_a_copy_of_the_old_accounts_gadget(
    db_uri: str, tmp_path
) -> None:
    from omnigent.gadgets.access import PermissionStoreGadgetAccess
    from omnigent.gadgets.service import GadgetService, GadgetViewer
    from omnigent.stores.gadget_store import NotFound as StoreNotFound
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    # Two servers share the accounts and permissions database, each with its own gadgets;
    # only server B cleans up after deleted accounts here.
    root_a = LocalGadgetStore(str(tmp_path / "a"))
    root_b, _, _ = _gadgets(db_uri, tmp_path / "b", accounts)
    old = accounts.create_user_with_password("alice", "alice-hash")
    old_registration = permissions.account_registrations(["alice"])["alice"]
    with account_authority_scope("alice", old.account_generation):
        board = GadgetService(
            root_a,
            PermissionStoreGadgetAccess(root_a, permissions),
            GadgetViewer("alice", old_registration),
        ).create("Synthetic board")

    # Deleted and registered again through server B, which never had the gadget.
    assert accounts.delete_user("alice") is True
    new = accounts.create_user_with_password("alice", "replacement-hash")

    access_a = PermissionStoreGadgetAccess(root_a, permissions)
    new_registration = permissions.account_registrations(["alice"])["alice"]
    assert new_registration != old_registration
    service_a = GadgetService(root_a, access_a, GadgetViewer("alice", new_registration))
    with account_authority_scope("alice", new.account_generation):
        with pytest.raises(StoreNotFound):
            service_a.get(board["id"])
        assert service_a.list_owned() == {"items": [], "next_cursor": None}
    assert root_b.get(board["id"]) is None


def test_a_name_registered_again_is_not_the_author_of_the_old_accounts_comments_or_actions(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    from omnigent.gadgets.service import GadgetError
    from tests.gadgets.test_gadget_service import publish_actions_board

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    _, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("bob", "bob-hash")
    accounts.create_user_with_password("alice", "alice-hash")
    board = publish_actions_board(service("bob").service)
    permissions.grant_for_gadget("alice", board["id"], 2)
    old_alice = service("alice")
    comment = old_alice.create_comment(
        board["id"], {"body": "Synthetic note", "anchor": {"text": "board"}, "operation_id": "c1"}
    )
    action = old_alice.request_action(
        board["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "a1"}
    )
    assert comment["yours"] is True

    # Alice's account goes, its name is registered again, and Bob shares the board afresh.
    assert accounts.delete_user("alice") is True
    accounts.create_user_with_password("alice", "replacement-hash")
    permissions.grant_for_gadget("alice", board["id"], 2)
    alice = service("alice")
    assert alice.registration != old_alice.registration

    # The comment and the action survive on Bob's board, but are not the new Alice's.
    [listed] = alice.list_comments(board["id"])["items"]
    assert listed["id"] == comment["id"] and listed["yours"] is False
    with pytest.raises(GadgetError) as denied:
        alice.update_comment(board["id"], comment["id"], {"body": "Changed"})
    assert denied.value.status == 403
    with pytest.raises(GadgetError):
        alice.delete_comment(board["id"], comment["id"])
    [entry] = alice.list_actions(board["id"])["items"]
    assert entry["id"] == action["id"] and entry["yours"] is False
    with pytest.raises(GadgetError):
        alice.update_action(
            board["id"],
            {
                "id": action["id"],
                "if_version": action["version"],
                "operation_id": "u1",
                "state": "running",
                "session_id": "s1",
            },
        )
    # Retrying the old account's requests makes new records, not replays of the old ones.
    again = alice.create_comment(
        board["id"], {"body": "Synthetic note", "anchor": {"text": "board"}, "operation_id": "c1"}
    )
    assert again["id"] != comment["id"] and again["yours"] is True


def test_a_password_change_keeps_the_accounts_comments_and_actions_its_own(
    db_uri: str, tmp_path
) -> None:
    from tests.gadgets.test_gadget_service import publish_actions_board

    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    _, _, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("bob", "bob-hash")
    accounts.create_user_with_password("alice", "alice-hash")
    board = publish_actions_board(service("bob").service)
    permissions.grant_for_gadget("alice", board["id"], 2)
    comment = service("alice").create_comment(
        board["id"], {"body": "Synthetic note", "anchor": {"text": "board"}, "operation_id": "c1"}
    )
    action = service("alice").request_action(
        board["id"], {"action": "run_card", "params": {"cardId": "c1"}, "operation_id": "a1"}
    )
    accounts.update_password("alice", "new-hash", revoke_sessions=True)
    alice = service("alice")
    assert alice.update_comment(board["id"], comment["id"], {"body": "Changed"})["yours"] is True
    [entry] = alice.list_actions(board["id"])["items"]
    assert entry["yours"] is True
    alice.update_action(
        board["id"],
        {
            "id": action["id"],
            "if_version": action["version"],
            "operation_id": "u1",
            "state": "running",
            "session_id": "s1",
        },
    )


def test_a_large_accounts_gadgets_go_a_bounded_number_per_call_and_the_rest_later(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    from omnigent.gadgets import accounts as gadget_accounts

    monkeypatch.setattr(gadget_accounts, "GADGETS_PER_CLEANUP", 2)
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    accounts.create_user_with_password("carol", "carol-hash")
    permissions.ensure_user("bob")
    old_alice = service("alice")
    boards = [old_alice.create(f"Synthetic board {n}") for n in range(5)]
    for board in boards:
        permissions.grant_for_gadget("bob", board["id"], 1)
    old_alice.pin(boards[0]["id"], {"pinned": True})
    deleted_store = store.delete
    deletions: list[str] = []

    def counting_delete(gadget_id: str) -> bool:
        deletions.append(gadget_id)
        return deleted_store(gadget_id)

    monkeypatch.setattr(store, "delete", counting_delete)

    def left() -> list[str]:
        return [g.id for g in store.iter_owned("alice", owner_registration=old_alice.registration)]

    # Deleting the account removes one bounded batch; the rest stays detached and shared
    # with no one until later calls finish it.
    assert accounts.delete_user("alice") is True
    assert len(deletions) == 2 and len(left()) == 3
    assert access.shared_with("bob").items == []

    # The name is registered again; its new gadget is never part of the old cleanup.
    accounts.create_user_with_password("alice", "replacement-hash")
    alice = service("alice")
    mine = alice.create("New board")
    assert len(deletions) <= 4

    # Other accounts' deletions and registrations move the old cleanup along to the end.
    for name in ("dave", "erin", "frank"):
        accounts.create_user_with_password(name, f"{name}-hash")
    assert left() == []
    assert len(deletions) == 5
    assert set(deletions) == {board["id"] for board in boards}
    assert store.get(mine["id"]) is not None
    assert alice.shelf()["pinned"] == []
    assert [g["id"] for g in alice.list_owned()["items"]] == [mine["id"]]
    assert permissions.list_for_gadget(boards[4]["id"]) == ([], None)


def test_a_cleanup_resumed_from_another_workspace_removes_that_workspaces_grants(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    from omnigent.db.db_models import workspace_scope
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.access import workspace_registration
    from omnigent.gadgets.service import GadgetService, GadgetViewer

    monkeypatch.setattr(gadget_accounts, "GADGETS_PER_CLEANUP", 1)
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, access, _ = _gadgets(db_uri, tmp_path, accounts)
    with workspace_scope(202):
        accounts.create_user_with_password("alice", "alice-hash")
        raw = permissions.account_registrations(["alice"]).get("alice")
        permissions.ensure_user("bob")
        registration = workspace_registration(raw)
        alice = GadgetService(store, access, GadgetViewer("alice", registration))
        boards = [alice.create(f"Synthetic board {n}") for n in range(3)]
        for board in boards:
            permissions.grant_for_gadget("bob", board["id"], 1)
        assert accounts.delete_user("alice") is True
        assert len(list(store.iter_owned("alice", owner_registration=registration))) == 2
    # Calls made in another workspace finish it, and the grants go with the gadgets.
    for _ in range(3):
        accounts._gadgets.resume()
    with workspace_scope(202):
        assert list(store.iter_owned("alice", owner_registration=registration)) == []
        for board in boards:
            assert permissions.list_for_gadget(board["id"]) == ([], None)


def test_a_cleanup_that_keeps_failing_never_blocks_the_others(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.stores.gadget_store import CorruptData

    monkeypatch.setattr(gadget_accounts, "GADGETS_PER_CLEANUP", 1)
    accounts = SqlAlchemyAccountStore(db_uri)
    permissions = GadgetGrantStore(db_uri)
    store, _, service = _gadgets(db_uri, tmp_path, accounts)
    gadgets = accounts._gadgets
    # An unreadable note, and a deleted account whose gadgets can't be listed, both at the
    # front of the queue.
    scope = store._user_dir(gadget_accounts._CLEANUP_SCOPE, gadget_accounts._CLEANUP_REGISTRATION)
    gadgets._note("mallory", None)
    (scope / gadget_accounts._segment(0) / ("0" * 32 + ".json")).write_text("{not json")
    real_page = store.list_owned_page

    def page(user_id, **kwargs):  # type: ignore[no-untyped-def]
        if user_id == "mallory":
            raise CorruptData("Invalid gadget metadata")
        return real_page(user_id, **kwargs)

    monkeypatch.setattr(store, "list_owned_page", page)
    accounts.create_user_with_password("alice", "alice-hash")
    permissions.ensure_user("bob")
    alice = service("alice")
    boards = [alice.create(f"Synthetic board {n}") for n in range(3)]
    assert accounts.delete_user("alice") is True
    for _ in range(3):
        gadgets.resume()
    assert list(store.iter_owned("alice", owner_registration=alice.registration)) == []
    assert all(store.get(board["id"]) is None for board in boards)


def test_a_long_cleanup_backlog_is_read_a_bounded_part_at_a_time(tmp_path, monkeypatch) -> None:
    """However many deleted accounts wait, one call reads a bounded part of the queue (at most
    two segments, and the newest when sending notes to the back) and works on a few notes;
    each goes to the back, so every note gets its turn."""
    import os

    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store import local as local_module
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class NoGrants:
        def forget(self, gadget_id: str) -> None:
            pass

    segment = 5
    monkeypatch.setattr(gadget_accounts, "QUEUE_SEGMENT_SIZE", segment)
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    gadgets = DeletedAccountGadgets(store, NoGrants())  # type: ignore[arg-type]
    users = [f"user{n:03d}" for n in range(150)]
    for user in users:
        gadgets._note(user, None)

    examined = 0
    real_scandir = os.scandir

    class CountingScandir:
        def __init__(self, path: object) -> None:
            self._it = real_scandir(path)

        def __enter__(self) -> CountingScandir:
            return self

        def __exit__(self, *exc: object) -> None:
            self._it.close()

        def __iter__(self) -> CountingScandir:
            return self

        def __next__(self) -> os.DirEntry[str]:
            nonlocal examined
            entry = next(self._it)
            examined += 1
            return entry

    monkeypatch.setattr(local_module.os, "scandir", CountingScandir)
    worked: list[str] = []

    def step(user_id: str, registration: str | None) -> bool:
        worked.append(user_id)
        return user_id in users[::3]  # Some finish; the others need more calls.

    monkeypatch.setattr(gadgets, "_step", step)
    for _ in range(80):
        examined = 0
        gadgets.resume()
        # Two front segments, and the newest once per note sent back: never the backlog.
        bound = (2 + gadget_accounts.PENDING_PER_RESUME) * (segment + 1)
        assert examined <= bound < len(users)
    # Every waiting cleanup got its turn, not only those at the front.
    assert set(worked) == set(users)
    assert all(worked.count(user) == 1 for user in users[::3])


def test_account_cleanup_refuses_a_store_whose_files_cannot_be_deleted(tmp_path) -> None:
    """The queue moves on only by removing finished notes, so on files it can't remove it
    would never reach the later ones: such a store is refused when the server starts."""
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class CreateOnly(LocalGadgetStore):
        def user_files(self, user_id, *, owner_registration=None):  # type: ignore[no-untyped-def]
            files = super().user_files(user_id, owner_registration=owner_registration)
            files.supports_delete = False  # type: ignore[misc]
            return files

    with pytest.raises(ValueError, match="can be deleted"):
        DeletedAccountGadgets(CreateOnly(str(tmp_path / "gadgets")), object())  # type: ignore[arg-type]


def test_account_cleanup_follows_an_empty_page_that_continues(tmp_path) -> None:
    """A store may answer a short or empty page that still continues: the next step, even
    from a cleanup made again (a restart), reads on from there instead of starting over."""
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class NoGrants:
        def forget(self, gadget_id: str) -> None:
            pass

    calls: list[str | None] = []

    class Sparse(LocalGadgetStore):
        def list_owned_page(self, owner_id, *, limit, after=None, owner_registration=None):  # type: ignore[no-untyped-def]
            calls.append(after)
            if after is None:
                return [], "next"
            return super().list_owned_page(
                owner_id, limit=limit, after=None, owner_registration=owner_registration
            )

    store = Sparse(str(tmp_path / "gadgets"))
    board = store.create("alice", "Synthetic board", owner_registration="r1")
    assert DeletedAccountGadgets(store, NoGrants())._step("alice", "r1") is False  # type: ignore[arg-type]
    # Made again, as after a restart: it carries on from the saved continuation.
    assert DeletedAccountGadgets(store, NoGrants())._step("alice", "r1") is True  # type: ignore[arg-type]
    assert calls[:2] == [None, "next"]  # (owns_any may list again after)
    assert store.get(board.id) is None


def test_a_backlog_of_unqueued_intents_is_read_a_bounded_part_at_a_time(
    tmp_path, monkeypatch
) -> None:
    """Deletions that found the queue busy leave intents; one call reads one intent segment
    and a few numbered markers, however many intents wait, and every intent is queued."""
    import os

    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store import local as local_module
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class NoGrants:
        def forget(self, gadget_id: str) -> None:
            pass

    size = 4
    monkeypatch.setattr(gadget_accounts, "INTENT_SEGMENT_SIZE", size)
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    gadgets = DeletedAccountGadgets(store, NoGrants())  # type: ignore[arg-type]
    queue = gadgets._queue()
    users = [f"user{n:03d}" for n in range(120)]
    for user in users:
        assert queue.intend(user, None, 0) is not None  # Never queued: the queue was busy.

    examined = 0
    real_scandir = os.scandir

    def counting_scandir(path: object):  # type: ignore[no-untyped-def]
        nonlocal examined
        name = os.fspath(path)  # type: ignore[arg-type]
        entries = list(real_scandir(path))
        if "/incoming" in name:
            examined += len(entries)
        return _Entries(entries)

    class _Entries(list):
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc: object) -> None:
            pass

    monkeypatch.setattr(local_module.os, "scandir", counting_scandir)
    for _ in range(len(users)):
        examined = 0
        queue.take_incoming(gadget_accounts.INCOMING_PER_RESUME)
        # One segment, listed at most twice: never the backlog.
        assert examined <= 2 * size < len(users)
    monkeypatch.setattr(local_module.os, "scandir", real_scandir)
    queued = {key for _, key in queue.front(len(users) + 1)}
    assert queued == {f"{gadget_accounts._key(user, None)}.json" for user in users}


def test_an_intent_written_as_its_segment_is_finished_is_recorded_again(tmp_path) -> None:
    """A writer that fills a slot after the reader marked its segment finished records the
    intent again in a later segment, so the reader's last pass can't miss it."""
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    files = DeletedAccountGadgets._files_of(store)
    queue = gadget_accounts._CleanupQueue(files)
    queue.intend("alice", None, 0)
    # The reader marked segment 1 finished, but the writer still sees it as the newest.
    files.mkdirs("incoming/done")
    files.create_json("incoming/done/1.json", {})

    path = queue.intend("bob", None, 0)

    assert path is not None and path.startswith("incoming/s00000002/")
    queue.take_incoming(8)
    assert f"{gadget_accounts._key('bob', None)}.json" in {key for _, key in queue.front(8)}


@pytest.mark.parametrize("how", ["busy", "stopped"])
def test_a_fenced_segment_whose_last_transfer_failed_is_finished_later(
    tmp_path, monkeypatch, how: str
) -> None:
    """An intent written just before the reader fenced its segment, whose last transfer then
    finds the queue busy or stops: reopened, ordinary calls still queue it."""
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store import Conflict as StoreConflict
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    monkeypatch.setattr(gadget_accounts, "INTENT_SEGMENT_SIZE", 1)
    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    files = DeletedAccountGadgets._files_of(store)
    queue = gadget_accounts._CleanupQueue(files)
    queue.intend("alice", None, 0)  # Segment 1, written just before the fence.
    queue.intend("bob", None, 0)  # Segment 2 started, so segment 1 can be retired.
    files.mkdirs("incoming/done")
    files.create_json("incoming/done/1.json", {})  # The reader fenced segment 1.
    real_add = gadget_accounts._CleanupQueue.add

    def failing_add(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if how == "busy":
            raise StoreConflict("The gadget cleanup queue is busy", reason="busy")
        raise KeyboardInterrupt  # The process stops mid-transfer.

    monkeypatch.setattr(gadget_accounts._CleanupQueue, "add", failing_add)
    with pytest.raises((StoreConflict, KeyboardInterrupt)):
        queue.take_incoming(8)
    monkeypatch.setattr(gadget_accounts._CleanupQueue, "add", real_add)

    reopened = gadget_accounts._CleanupQueue(DeletedAccountGadgets._files_of(store))
    for _ in range(3):
        reopened.take_incoming(8)
    queued = {key for _, key in reopened.front(8)}
    assert f"{gadget_accounts._key('alice', None)}.json" in queued
    assert f"{gadget_accounts._key('bob', None)}.json" in queued


def test_a_head_update_stopped_half_way_leaves_the_queue_readable(tmp_path, monkeypatch) -> None:
    """A note sent to a later segment, then the queue's head update stops while retiring the
    emptied first segment: reopened, the queue still finds the note, and only its cleanup
    runs."""
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class NoGrants:
        def forget(self, gadget_id: str) -> None:
            pass

    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    files = DeletedAccountGadgets._files_of(store)
    stop = {"heads": False}

    class StoppingFiles:
        def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
            return getattr(files, name)

        def create_json(self, path: str, value: dict) -> None:
            if stop["heads"] and path.startswith("queue/head"):
                raise OSError("stopped writing the head")
            files.create_json(path, value)

    queue = gadget_accounts._CleanupQueue(StoppingFiles())  # type: ignore[arg-type]
    queue.add("alice", "r1", 0)
    name = f"{gadget_accounts._key('alice', 'r1')}.json"
    queue.to_back(0, name, queue.read(0, name))  # Now in segment 1; segment 0 is empty.
    stop["heads"] = True
    with pytest.raises(OSError):
        queue.front(5)  # Retiring segment 0 stops at the head update.

    worked: list[str | None] = []
    reopened = DeletedAccountGadgets(LocalGadgetStore(str(tmp_path / "gadgets")), NoGrants())  # type: ignore[arg-type]
    monkeypatch.setattr(reopened, "_step", lambda user, registration: worked.append(user) or True)
    reopened.resume()
    assert worked == ["alice"]
    assert reopened._queue().front(5) == []


def test_a_deletion_whose_queueing_fails_is_cleaned_up_after_a_restart(
    db_uri: str, tmp_path, monkeypatch
) -> None:
    """The queue is busy when the deletion tries to queue its cleanup: the intent it wrote
    first gets it queued later, so a restarted server finishes it without the name being
    registered again."""
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store import Conflict as StoreConflict

    monkeypatch.setattr(gadget_accounts, "GADGETS_PER_CLEANUP", 1)
    accounts = SqlAlchemyAccountStore(db_uri)
    store, access, service = _gadgets(db_uri, tmp_path, accounts)
    accounts.create_user_with_password("alice", "alice-hash")
    alice = service("alice")
    boards = [alice.create(f"Synthetic board {n}") for n in range(3)]

    real_add = gadget_accounts._CleanupQueue.add
    calls = {"n": 0}

    def busy_once(self, user_id, registration, workspace, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise StoreConflict("The gadget cleanup queue is busy", reason="busy")
        return real_add(self, user_id, registration, workspace, **kwargs)

    monkeypatch.setattr(gadget_accounts._CleanupQueue, "add", busy_once)
    assert accounts.delete_user("alice") is True
    left = list(store.iter_owned("alice", owner_registration=alice.registration))
    assert left, "one call deletes one gadget here, so some are left for later"

    restarted = DeletedAccountGadgets(store, access)
    for _ in range(5):
        restarted.resume()
    assert list(store.iter_owned("alice", owner_registration=alice.registration)) == []
    assert all(store.get(board["id"]) is None for board in boards)


def test_an_account_named_like_the_cleanup_scope_keeps_the_others_cleanup_notes(
    tmp_path, monkeypatch
) -> None:
    """A user ID may be any string, even the cleanup notes' scope name; deleting that
    account's files must not delete the notes other deleted accounts' cleanups wait on."""
    from omnigent.gadgets import accounts as gadget_accounts
    from omnigent.gadgets.accounts import DeletedAccountGadgets
    from omnigent.stores.gadget_store.local import LocalGadgetStore

    class NoGrants:
        def forget(self, gadget_id: str) -> None:
            pass

    store = LocalGadgetStore(str(tmp_path / "gadgets"))
    gadgets = DeletedAccountGadgets(store, NoGrants())  # type: ignore[arg-type]
    gadgets._note("mallory", None)
    # Mallory's cleanup is not run in this call, so only the scope-named account's goes.
    monkeypatch.setattr(gadget_accounts, "PENDING_PER_RESUME", 0)
    assert gadgets.delete_owned(gadget_accounts._CLEANUP_SCOPE, None) is True

    resumed: list[str] = []
    monkeypatch.setattr(gadget_accounts, "PENDING_PER_RESUME", 4)
    monkeypatch.setattr(
        gadgets, "_step", lambda user_id, registration: resumed.append(user_id) or True
    )
    gadgets.resume()
    assert resumed == ["mallory"]
