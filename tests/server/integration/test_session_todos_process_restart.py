import json
import subprocess
import sys

_SERVER_PROBE = r"""
import json, sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
database, mode = sys.argv[1:3]
store = SqlAlchemyConversationStore(database); agents = SqlAlchemyAgentStore(database)
if mode == "report":
    agent = agents.create(agent_id="a" * 32, name="fixture", bundle_location="fixture")
    session_id = store.create_conversation(title="Plan review", agent_id=agent.id).id
else:
    session_id = sys.argv[3]
app = FastAPI(); router = create_sessions_router(conversation_store=store, agent_store=agents)
app.include_router(router, prefix="/v1")
with TestClient(app) as client:
    def event(todos): return {"type": "external_session_todos", "data": {"todos": todos}}
    if mode == "report":
        todos = [{"content": "Verify restart", "status": "in_progress", "activeForm": "Verifying"}]
        client.post(f"/v1/sessions/{session_id}/events", json=event(todos))
    if mode == "clear":
        client.post(f"/v1/sessions/{session_id}/events", json=event([]))
    data = client.get(f"/v1/sessions/{session_id}").json()
    print(json.dumps({"id": session_id, "todos": data["todos"], "items": data["items"]}))
"""


def test_plan_hydrates_after_a_real_server_process_restart(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'restart.db'}"

    def run(mode: str, *args: str) -> dict:
        result = subprocess.check_output(
            [sys.executable, "-c", _SERVER_PROBE, database, mode, *args],
            text=True,
            timeout=45,
        )
        return json.loads(result.strip().splitlines()[-1])

    before = run("report")
    assert run("read", before["id"]) == before and before["items"] == []
    assert before["todos"][0]["status"] == "in_progress"
    cleared = run("clear", before["id"])
    assert run("read", before["id"]) == cleared and cleared["todos"] == []
