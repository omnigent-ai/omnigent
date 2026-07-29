"""
Unit and lifecycle tests for DeferredActionManager and MemoryDeferredActionStore.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from omnigent.runtime.deferred.manager import (
    DeferredActionError,
    DeferredActionManager,
    DeferredExpiredError,
    DeferredStateError,
    HashDriftError,
)
from omnigent.runtime.deferred.store import MemoryDeferredActionStore


@pytest.mark.asyncio
async def test_freeze_and_approve_lifecycle():
    """Verify freeze -> approve -> execute happy path lifecycle."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    action = await manager.freeze(
        tool="sys_os_edit",
        arguments={"file": "test.txt", "text": "hello"},
        base_hash="hash_base_1",
        session_id="session_100",
        actor="test_user",
    )

    assert action.status == "PENDING"
    assert action.id.startswith("def_")

    # Approve with matching base_hash
    approved_action = await manager.approve(
        action.id,
        actor="admin",
        current_base_hash="hash_base_1",
    )

    assert approved_action.status == "APPROVED"

    # Execute callback
    executed = False

    async def _dummy_exec():
        nonlocal executed
        executed = True

    final_action = await manager.execute(action.id, _dummy_exec)

    assert final_action.status == "EXECUTED"
    assert executed is True

    # Audit events check
    audit_events = await store.list_audit_events(action.id)
    event_types = [evt.event_type for evt in audit_events]
    assert event_types == ["CREATE", "APPROVE", "EXECUTE"]


@pytest.mark.asyncio
async def test_executor_crash_transitions_to_failed():
    """Verify exception inside executor transitions action status to FAILED."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    action = await manager.freeze(
        tool="sys_os_shell",
        arguments={"command": "exit 1"},
        base_hash="hash_base_1",
        session_id="session_101",
    )

    await manager.approve(
        action.id,
        actor="admin",
        current_base_hash="hash_base_1",
    )

    async def _crashing_exec():
        raise RuntimeError("Command execution failed")

    with pytest.raises(RuntimeError, match="Command execution failed"):
        await manager.execute(action.id, _crashing_exec)

    failed_action = await store.get_action(action.id)
    assert failed_action.status == "FAILED"
    assert "Command execution failed" in (failed_action.error_message or "")


@pytest.mark.asyncio
async def test_hash_drift_detection_fail_closed():
    """Verify modifying base hash before approval raises HashDriftError and sets HASH_DRIFT."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    action = await manager.freeze(
        tool="sys_os_write",
        arguments={"path": "main.py", "content": "v1"},
        base_hash="hash_original",
        session_id="session_102",
    )

    with pytest.raises(HashDriftError, match="State drift detected"):
        await manager.approve(
            action.id,
            actor="admin",
            current_base_hash="hash_drifted_state",
        )

    drifted_action = await store.get_action(action.id)
    assert drifted_action.status == "HASH_DRIFT"


@pytest.mark.asyncio
async def test_expiration_fail_closed():
    """Verify approval after expiration date raises DeferredExpiredError and sets EXPIRED."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    # Freeze action with 0 second expiration budget
    action = await manager.freeze(
        tool="sys_os_write",
        arguments={"path": "main.py"},
        base_hash="hash_1",
        session_id="session_103",
        expiration_seconds=-10,  # Already expired
    )

    with pytest.raises(DeferredExpiredError, match="has expired"):
        await manager.approve(
            action.id,
            actor="admin",
            current_base_hash="hash_1",
        )

    expired_action = await store.get_action(action.id)
    assert expired_action.status == "EXPIRED"


@pytest.mark.asyncio
async def test_rejection_flow():
    """Verify rejecting action sets status REJECTED."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    action = await manager.freeze(
        tool="sys_os_shell",
        arguments={"cmd": "rm -rf /"},
        base_hash="hash_1",
        session_id="session_104",
    )

    rejected_action = await manager.reject(
        action.id,
        actor="security_admin",
        reason="Dangerous command",
    )

    assert rejected_action.status == "REJECTED"
    assert rejected_action.reason == "Dangerous command"


@pytest.mark.asyncio
async def test_concurrent_approvals():
    """Verify concurrent approve calls evaluate safely without race condition failures."""
    store = MemoryDeferredActionStore()
    manager = DeferredActionManager(store=store)

    action = await manager.freeze(
        tool="sys_os_edit",
        arguments={"path": "a.txt"},
        base_hash="hash_1",
        session_id="session_105",
    )

    results = await asyncio.gather(
        manager.approve(action.id, actor="u1", current_base_hash="hash_1"),
        manager.approve(action.id, actor="u2", current_base_hash="hash_1"),
        manager.approve(action.id, actor="u3", current_base_hash="hash_1"),
    )

    for res in results:
        assert res.status == "APPROVED"
