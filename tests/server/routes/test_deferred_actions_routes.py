"""
Integration tests for deferred actions REST API endpoints.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.runtime.deferred.manager import DeferredActionManager
from omnigent.runtime.deferred.store import MemoryDeferredActionStore, set_deferred_store
from omnigent.server.routes.deferred_actions import router as deferred_actions_router

app = FastAPI()
app.include_router(deferred_actions_router)
client = TestClient(app)


@pytest.fixture(autouse=True)
def _setup_test_store():
    """Inject fresh memory store for each route test."""
    store = MemoryDeferredActionStore()
    set_deferred_store(store)
    yield store
    set_deferred_store(None)


@pytest.mark.asyncio
async def test_deferred_actions_api_routes():
    """Test list, detail, approve, and reject REST routes."""
    manager = DeferredActionManager()
    action = await manager.freeze(
        tool="sys_os_shell",
        arguments={"command": "npm run build"},
        base_hash="hash_base_999",
        session_id="session_api_test",
        reason="Build required",
    )

    # 1. List for session
    res = client.get(f"/v1/sessions/session_api_test/deferred_actions")
    assert res.status_code == 200
    data = res.json()
    assert len(data["deferred_actions"]) == 1
    assert data["deferred_actions"][0]["id"] == action.id

    # 2. Get detail & audit events
    res = client.get(f"/v1/deferred_actions/{action.id}")
    assert res.status_code == 200
    detail = res.json()
    assert detail["action"]["id"] == action.id
    assert len(detail["audit_events"]) == 1

    # 3. Approve action via POST
    res = client.post(
        f"/v1/deferred_actions/{action.id}/approve",
        json={"actor": "reviewer", "current_base_hash": "hash_base_999"},
    )
    assert res.status_code == 200
    approved_resp = res.json()
    assert approved_resp["action"]["status"] == "APPROVED"

    # 4. Attempt approve with hash mismatch (conflict)
    action2 = await manager.freeze(
        tool="sys_os_write",
        arguments={"path": "file.txt"},
        base_hash="hash_original",
        session_id="session_api_test",
    )
    res = client.post(
        f"/v1/deferred_actions/{action2.id}/approve",
        json={"actor": "reviewer", "current_base_hash": "hash_drifted"},
    )
    assert res.status_code == 409

    # 5. Reject action via POST
    action3 = await manager.freeze(
        tool="sys_os_write",
        arguments={"path": "file2.txt"},
        base_hash="hash_3",
        session_id="session_api_test",
    )
    res = client.post(
        f"/v1/deferred_actions/{action3.id}/reject",
        json={"actor": "reviewer", "reason": "Not allowed"},
    )
    assert res.status_code == 200
    assert res.json()["action"]["status"] == "REJECTED"
