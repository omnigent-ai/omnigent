"""Applying an automation's grants to its run sessions (:mod:`.run_sharing`).

Grants use the real permission and scheduled task stores. How a run calls these is
covered in ``test_claimed_fire.py`` and ``test_fire.py``.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
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
from omnigent.stores import permission_store as permission_store_module
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

    await sync_continued_session(
        task, continued, forced=True, permission_store=permissions, **common
    )
    await share_new_run_session(task, fresh, permission_store=permissions, **common)
    await share_new_run_session(task, fresh, permission_store=None, **common)

    assert _levels(permissions, continued.id) == {"carol@example.com": LEVEL_READ}
    assert _levels(permissions, fresh.id) == {}


async def _sync(
    permissions: SqlAlchemyPermissionStore,
    task: ScheduledTask,
    session: _FakeConversation,
    *,
    app_state: Any = None,
) -> None:
    await sync_continued_session(
        task,
        session,
        forced=False,
        permission_store=permissions,
        app_state=app_state,
        host_registry=None,
    )


async def test_a_sync_reads_every_page_of_both_grant_lists(permissions, task, monkeypatch) -> None:
    # Two grants a page puts Bob's Edit and Carol's hand-made share on the session's
    # second page, and Zoe on the automation's.
    monkeypatch.setattr(permission_store_module, "MAX_GRANTS_PAGE_SIZE", 2)
    permissions.ensure_user("zoe@example.com")
    permissions.grant_for_scheduled_task("zoe@example.com", task.id, LEVEL_READ)
    session = _session(
        permissions,
        **{
            "amy@example.com": LEVEL_READ,
            "ann@example.com": LEVEL_READ,
            "bob@example.com": LEVEL_EDIT,
            "carol@example.com": LEVEL_READ,
            RESERVED_USER_LOCAL: LEVEL_OWNER,
        },
    )

    await _sync(permissions, task, session)

    assert _levels(permissions, session.id) == {
        "alice@example.com": LEVEL_READ,
        "bob@example.com": LEVEL_EDIT,
        "zoe@example.com": LEVEL_READ,
        RESERVED_USER_LOCAL: LEVEL_OWNER,
    }


@pytest.mark.parametrize("level", [LEVEL_EDIT, LEVEL_MANAGE])
async def test_a_share_made_after_the_grantee_was_found_without_one_is_not_lowered(
    permissions, task, announced, monkeypatch, level
) -> None:
    session = _session(permissions, **{RESERVED_USER_LOCAL: LEVEL_OWNER})
    get = permissions.get

    def share_by_hand_once_looked_up(user_id: str, session_id: str) -> Any:
        held = get(user_id, session_id)
        if user_id == "alice@example.com":
            # The owner shares with Alice by hand after the sync found she had no share.
            permissions.grant(user_id, session_id, level)
        return held

    monkeypatch.setattr(permissions, "get", share_by_hand_once_looked_up)
    await _sync(permissions, task, session)

    assert _levels(permissions, session.id) == {
        RESERVED_USER_LOCAL: LEVEL_OWNER,
        "alice@example.com": level,
        "bob@example.com": LEVEL_READ,
    }
    assert announced == [("bob@example.com", session.id)]


async def test_a_sync_keeps_a_grant_the_database_matches_to_a_grantee(
    permissions, task, db_uri
) -> None:
    """MySQL's collation matches ``Bob@`` to the listed ``bob@``; other engines don't."""
    session = _session(
        permissions, **{RESERVED_USER_LOCAL: LEVEL_OWNER, "Bob@example.com": LEVEL_EDIT}
    )

    await _sync(permissions, task, session)

    variant = (
        {"Bob@example.com": LEVEL_EDIT}
        if permissions._engine.dialect.name == "mysql"
        # An unlisted ID is revoked, and the listed one is added.
        else {"bob@example.com": LEVEL_READ}
    )
    assert _levels(permissions, session.id) == {
        RESERVED_USER_LOCAL: LEVEL_OWNER,
        "alice@example.com": LEVEL_READ,
        **variant,
    }


@pytest.mark.parametrize("upgraded", [False, True], ids=["unchanged", "upgraded"])
@pytest.mark.parametrize("fault", ["revoked", "recheck_failed"])
async def test_a_share_is_taken_back_unless_the_grant_outlasts_its_write(
    permissions, task, announced, monkeypatch, fault, upgraded
) -> None:
    """A revoke that lands between the run's check and its write, after which the
    revoke's own sync finds nothing to remove, must not leave Alice on the session.
    Taking the share back never removes an upgrade made by hand since the write."""
    session = _session(permissions, **{RESERVED_USER_LOCAL: LEVEL_OWNER})
    grant_if_absent = permissions.grant_if_absent
    resolve = permissions.resolve_scheduled_task_access
    fail_next_check = False

    def write(user_id: str, conversation_id: str, level: int) -> bool:
        nonlocal fail_next_check
        if user_id == "alice@example.com":
            if fault == "revoked":
                permissions.revoke_for_scheduled_task(user_id, task.id)
            else:
                fail_next_check = True
        written = grant_if_absent(user_id, conversation_id, level)
        if upgraded and user_id == "alice@example.com":
            # The owner upgrades Alice by hand before the run rechecks her grant.
            permissions.grant(user_id, conversation_id, LEVEL_EDIT)
        return written

    def check(principals: list[str], scheduled_task_id: str) -> Any:
        nonlocal fail_next_check
        if fail_next_check:
            fail_next_check = False
            raise RuntimeError("check failed")
        return resolve(principals, scheduled_task_id)

    monkeypatch.setattr(permissions, "grant_if_absent", write)
    monkeypatch.setattr(permissions, "resolve_scheduled_task_access", check)
    await _sync(permissions, task, session)

    upgrade = {"alice@example.com": LEVEL_EDIT} if upgraded else {}
    assert _levels(permissions, session.id) == {
        RESERVED_USER_LOCAL: LEVEL_OWNER,
        "bob@example.com": LEVEL_READ,
        **upgrade,
    }
    assert announced == [("bob@example.com", session.id)]


async def test_a_cancelled_share_still_takes_back_a_grant_revoked_during_its_write(
    permissions, task, announced, monkeypatch
) -> None:
    session = _session(permissions)
    grant_if_absent = permissions.grant_if_absent
    revoke_if_level = permissions.revoke_if_level
    writing, release = threading.Event(), threading.Event()
    taken_back: list[bool] = []

    def held_write(user_id: str, conversation_id: str, level: int) -> bool:
        writing.set()
        assert release.wait(10)
        return grant_if_absent(user_id, conversation_id, level)

    def take_back(user_id: str, conversation_id: str, level: int) -> bool:
        taken_back.append(revoke_if_level(user_id, conversation_id, level))
        return taken_back[-1]

    monkeypatch.setattr(permissions, "grant_if_absent", held_write)
    monkeypatch.setattr(permissions, "revoke_if_level", take_back)
    sharing = asyncio.create_task(
        share_new_run_session(
            task, session, permission_store=permissions, app_state=None, host_registry=None
        )
    )
    assert await asyncio.to_thread(writing.wait, 10)
    # Alice's grant is revoked while her share is being written, and the run is cancelled.
    permissions.revoke_for_scheduled_task("alice@example.com", task.id)
    sharing.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await sharing

    # The cancel propagated only once the written share had been taken back.
    assert taken_back == [True]
    assert _levels(permissions, session.id) == {}
    assert announced == []


async def test_a_failed_grantee_check_keeps_the_share(permissions, task, monkeypatch) -> None:
    grants = {RESERVED_USER_LOCAL: LEVEL_OWNER, "carol@example.com": LEVEL_READ}
    session = _session(permissions, **grants)
    monkeypatch.setattr(
        permissions,
        "resolve_scheduled_task_access",
        Mock(side_effect=RuntimeError("check failed")),
    )

    await _sync(permissions, task, session)

    assert _levels(permissions, session.id) == grants


async def test_a_sync_never_revokes_an_owner_grant_or_the_automation_owner(
    permissions, task
) -> None:
    # Each is kept by its own rule: an Owner grant, and the automation owner's ID.
    session = _session(
        permissions,
        **{
            "dana@example.com": LEVEL_OWNER,
            RESERVED_USER_LOCAL: LEVEL_MANAGE,
            "carol@example.com": LEVEL_READ,
        },
    )

    await _sync(permissions, task, session)

    assert _levels(permissions, session.id) == {
        "dana@example.com": LEVEL_OWNER,
        RESERVED_USER_LOCAL: LEVEL_MANAGE,
        **dict.fromkeys(GRANTEES, LEVEL_READ),
    }


async def test_a_sync_still_revokes_when_the_sharing_policy_refuses_grants(
    permissions, task
) -> None:
    session = _session(
        permissions, **{RESERVED_USER_LOCAL: LEVEL_OWNER, "carol@example.com": LEVEL_READ}
    )

    await _sync(
        permissions, task, session, app_state=SimpleNamespace(sharing_mode=lambda: SharingMode.OFF)
    )

    assert _levels(permissions, session.id) == {RESERVED_USER_LOCAL: LEVEL_OWNER}


@pytest.mark.parametrize("failing", ["shared_check", "second_session_page"])
async def test_a_failed_grant_read_revokes_nothing_unchecked(
    permissions, task, monkeypatch, failing
) -> None:
    monkeypatch.setattr(permission_store_module, "MAX_GRANTS_PAGE_SIZE", 1)
    if failing == "shared_check":
        list_task_page = permissions.list_for_scheduled_task
        failed = False

        def fail_the_first_read(task_id: str, **kwargs: Any) -> Any:
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("could not tell whether the automation is shared")
            return list_task_page(task_id, **kwargs)

        monkeypatch.setattr(permissions, "list_for_scheduled_task", fail_the_first_read)
    else:
        list_session_page = permissions.list_for_session

        def fail_after_the_first_page(session_id: str, **kwargs: Any) -> Any:
            if kwargs.get("after_user_id") is not None:
                raise RuntimeError("second page failed")
            return list_session_page(session_id, **kwargs)

        monkeypatch.setattr(permissions, "list_for_session", fail_after_the_first_page)
    session = _session(
        permissions,
        **{
            "amy@example.com": LEVEL_READ,
            "carol@example.com": LEVEL_READ,
            RESERVED_USER_LOCAL: LEVEL_OWNER,
        },
    )

    await _sync(permissions, task, session)

    # Amy, on the only page read, was checked and revoked; Carol was never checked.
    amy = {"amy@example.com": LEVEL_READ} if failing == "shared_check" else {}
    assert _levels(permissions, session.id) == {
        **amy,
        "carol@example.com": LEVEL_READ,
        RESERVED_USER_LOCAL: LEVEL_OWNER,
        # Grantees are still added.
        **dict.fromkeys(GRANTEES, LEVEL_READ),
    }


@pytest.mark.parametrize(
    "case", ["owned", "owned_without_grants", "not_owned", "new_session_each_run"]
)
async def test_a_grant_change_syncs_only_a_continued_session_its_owner_owns(
    permissions, task, case
) -> None:
    if case == "owned_without_grants":
        for grantee in GRANTEES:
            permissions.revoke_for_scheduled_task(grantee, task.id)
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

    # The sync is forced: removing the last grantee still revokes the hand-made share.
    expected = {
        "owned": {**owner, **dict.fromkeys(GRANTEES, LEVEL_READ)},
        "owned_without_grants": owner,
    }.get(case, owner | hand_made)
    assert _levels(permissions, session.id) == expected
