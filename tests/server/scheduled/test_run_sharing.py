"""Applying an automation's grants to its run sessions (:mod:`.run_sharing`).

Grants use the real permission and scheduled task stores. How a run calls these is
covered in ``test_claimed_fire.py`` and ``test_fire.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_OWNER,
    LEVEL_READ,
    RESERVED_USER_LOCAL,
    SharingMode,
)
from omnigent.server.scheduled import run_sharing
from omnigent.server.scheduled.run_sharing import (
    share_new_run_session,
    sync_continued_session,
    sync_task_session,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests.server.scheduled.test_fire import (
    FakeHostRegistry,
    _ExistingConversationStore,
    _FakeConversation,
)

_HOST_ID = uuid.uuid5(uuid.NAMESPACE_DNS, "host").hex
GRANTEES = ["alice@example.com", "bob@example.com"]


@pytest.fixture
def permissions(db_uri: str) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(db_uri)


@pytest.fixture
def task(db_uri: str, permissions: SqlAlchemyPermissionStore) -> ScheduledTask:
    """An automation shared with :data:`GRANTEES`."""
    created = SqlAlchemyScheduledTaskStore(db_uri).create(
        uuid.uuid4().hex,
        "nightly",
        "do the thing",
        "FREQ=HOURLY",
        None,
        uuid.uuid4().hex,
        "UTC",
        workspace="/repo",
        host_id=_HOST_ID,
    )
    for grantee in GRANTEES:
        permissions.ensure_user(grantee)
        permissions.grant_for_scheduled_task(grantee, created.id, LEVEL_READ)
    return created


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str | None, str]]:
    calls: list[tuple[str | None, str]] = []
    monkeypatch.setattr(run_sharing, "_announce_session_added", lambda *args: calls.append(args))
    return calls


def _session(permissions: SqlAlchemyPermissionStore, **grants: int) -> _FakeConversation:
    """A session on the task's host, holding *grants* (grantee to level)."""
    session = _FakeConversation(
        id=uuid.uuid4().hex, agent_id="ag_1", host_id=_HOST_ID, workspace="/repo"
    )
    for user_id, level in grants.items():
        permissions.ensure_user(user_id)
        permissions.grant(user_id, session.id, level)
    return session


def _levels(permissions: SqlAlchemyPermissionStore, session_id: str) -> dict[str, int]:
    grants, _ = permissions.list_for_session(session_id)
    return {grant.user_id: grant.level for grant in grants}


@pytest.mark.parametrize(
    "mode,target,workspace,shared",
    [
        (SharingMode.ON, "connected_host", "/repo", True),
        (SharingMode.READ_ONLY, "connected_host", "/repo", True),
        (SharingMode.OFF, "connected_host", "/repo", False),
        (SharingMode.RESTRICTED_READ_ONLY, "connected_host", "/home/alice/repo", True),
        (SharingMode.RESTRICTED_READ_ONLY, "connected_host", "/home/alice", False),
        # Without a known workspace only a server-provisioned sandbox is shared.
        (SharingMode.RESTRICTED_READ_ONLY, "connected_host", None, False),
        (SharingMode.RESTRICTED_READ_ONLY, "managed_sandbox", None, True),
        (SharingMode.RESTRICTED_READ_ONLY, "sandbox_host", None, True),
    ],
)
async def test_the_sharing_policy_decides_whether_a_run_session_is_shared(
    permissions, task, announced, mode, target, workspace, shared
) -> None:
    session = replace(_session(permissions), workspace=workspace)
    sandbox_host = target == "sandbox_host"

    await share_new_run_session(
        replace(task, execution_target="connected_host" if sandbox_host else target),
        session,
        permission_store=permissions,
        app_state=SimpleNamespace(sharing_mode=lambda: mode),
        host_registry=FakeHostRegistry(sandbox_hosts={_HOST_ID}) if sandbox_host else None,
    )

    assert _levels(permissions, session.id) == (
        dict.fromkeys(GRANTEES, LEVEL_READ) if shared else {}
    )
    assert announced == ([(grantee, session.id) for grantee in GRANTEES] if shared else [])


async def test_a_store_without_automation_grants_changes_no_session(
    permissions, task, monkeypatch
) -> None:
    monkeypatch.setattr(SqlAlchemyPermissionStore, "supports_scheduled_task_grants", False)
    continued = _session(permissions, **{"carol@example.com": LEVEL_READ})
    fresh = _session(permissions)
    common: dict[str, Any] = {"app_state": None, "host_registry": None}

    await sync_continued_session(task, continued, permission_store=permissions, **common)
    await share_new_run_session(task, fresh, permission_store=permissions, **common)
    await share_new_run_session(task, fresh, permission_store=None, **common)

    assert _levels(permissions, continued.id) == {"carol@example.com": LEVEL_READ}
    assert _levels(permissions, fresh.id) == {}


async def test_a_case_variant_of_a_grantee_is_neither_lowered_nor_revoked(
    permissions, task
) -> None:
    # MySQL keys match case variants to one row.
    session = _session(permissions, **{"Alice@example.com": LEVEL_EDIT})

    await sync_continued_session(
        task, session, permission_store=permissions, app_state=None, host_registry=None
    )

    assert _levels(permissions, session.id) == {
        "Alice@example.com": LEVEL_EDIT,
        "bob@example.com": LEVEL_READ,
    }


@pytest.mark.parametrize("case", ["owned", "not_owned", "new_session_each_run"])
async def test_a_grant_change_syncs_only_a_continued_session_its_owner_owns(
    permissions, task, case
) -> None:
    hand_made = {"carol@example.com": LEVEL_READ}
    owner = {RESERVED_USER_LOCAL: LEVEL_OWNER} if case != "not_owned" else {}
    session = _session(permissions, **owner, **hand_made)
    session_mode = "new" if case == "new_session_each_run" else "continue"

    await sync_task_session(
        replace(task, session_mode=session_mode, session_id=session.id),
        conversation_store=_ExistingConversationStore(session),
        permission_store=permissions,
        app_state=None,
        host_registry=None,
    )

    expected = (
        {**owner, **dict.fromkeys(GRANTEES, LEVEL_READ)} if case == "owned" else owner | hand_made
    )
    assert _levels(permissions, session.id) == expected
