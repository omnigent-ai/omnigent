"""Exercise Plan report -> process exit -> new Server HTTP snapshot."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SERVER_PROBE = r"""
import json
import sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

database, mode = sys.argv[1:3]
store = SqlAlchemyConversationStore(database)
agents = SqlAlchemyAgentStore(database)
permissions = SqlAlchemyPermissionStore(database)
if mode == "report":
    agent_id = "087b7cb7ac30abf4debfaa578d052ec6"
    agents.create(agent_id=agent_id, name="review fixture", bundle_location="fixture/bundle")
    session_id = store.create_conversation(title="Plan review", agent_id=agent_id).id
    permissions.ensure_user("reviewer@example.com")
    permissions.grant("reviewer@example.com", session_id, LEVEL_OWNER)
else:
    session_id = sys.argv[3]
app = FastAPI()
app.include_router(create_sessions_router(
    conversation_store=store, agent_store=agents,
    auth_provider=UnifiedAuthProvider(source="header"), permission_store=permissions,
), prefix="/v1")
with TestClient(app, headers={"X-Forwarded-Email": "reviewer@example.com"}) as client:
    if mode == "report":
        reported = client.post(f"/v1/sessions/{session_id}/events", json={
            "type": "external_session_todos", "data": {"todos": [
                {"content": "Verify restart", "status": "in_progress", "activeForm": "Verifying"}
            ]}
        })
        assert reported.status_code == 202, reported.text
    snapshot = client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    data = snapshot.json()
    print(json.dumps({"id": session_id, "todos": data["todos"], "items": data["items"]}))
"""


def test_plan_hydrates_after_a_real_server_process_restart(tmp_path: Path) -> None:
    database = f"sqlite:///{tmp_path / 'restart.db'}"

    def run(mode: str, *args: str) -> dict:
        result = subprocess.run(
            [sys.executable, "-c", _SERVER_PROBE, database, mode, *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )
        return json.loads(result.stdout.strip().splitlines()[-1])

    before = run("report")
    after = run("read", before["id"])
    assert before == after
    assert after["todos"] == [
        {"content": "Verify restart", "status": "in_progress", "activeForm": "Verifying"},
    ]
    assert after["items"] == []
