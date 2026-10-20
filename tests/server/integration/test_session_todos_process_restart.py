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
from omnigent.stores.artifact_store.local import LocalArtifactStore
database, artifacts_path, mode = sys.argv[1:4]
store = SqlAlchemyConversationStore(database); agents = SqlAlchemyAgentStore(database)
if mode == "report":
    agent = agents.create(agent_id="a" * 32, name="fixture", bundle_location="fixture")
    session_id = store.create_conversation(title="Plan review", agent_id=agent.id).id
else:
    session_id = sys.argv[4]
app = FastAPI(); router = create_sessions_router(conversation_store=store, agent_store=agents,
    artifact_store=LocalArtifactStore(artifacts_path))
app.include_router(router, prefix="/v1")
with TestClient(app) as client:
    def event(todos): return {"type": "external_session_todos", "data": {"todos": todos}}
    prs = [{"provider": "github", "host": "github.com", "repository": "example/project",
            "number": 42, "url": "https://github.com/example/project/pull/42",
            "relationship": "created", "source": "native_hook", "first_seen_at": 1,
            "last_seen_at": 2, "title": "Saved change"}]
    if mode == "report":
        response = client.post(f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_prs", "data": {"prs": prs, "updated_at_ns": 1}})
        assert response.status_code == 202, response.text
        response = client.post(f"/v1/sessions/{session_id}/events", json={
            "type": "external_session_pr_snapshot", "data": {
                "pr_url": prs[0]["url"], "captured_at_ns": 3_000_000_000,
                "info": {"pr": {**prs[0], "body": "Saved description",
                    "comments": [{"body": "Saved comment"}]}},
                "changes": {"data": [{"path": "test.py", "status": "modified"}]},
                "diff": {"patch": "Saved patch"}}})
        assert response.status_code == 202 and response.json()["saved"], response.text
        todos = [{"content": "Verify restart", "status": "in_progress", "activeForm": "Verifying"}]
        client.post(f"/v1/sessions/{session_id}/events", json=event(todos))
    if mode == "clear":
        response = client.post(f"/v1/sessions/{session_id}/events",
            json={"type": "external_session_prs", "data": {"prs": [], "updated_at_ns": 2}})
        assert response.status_code == 202, response.text
        client.post(f"/v1/sessions/{session_id}/events", json=event([]))
    data = client.get(f"/v1/sessions/{session_id}").json()
    assert data["pull_requests_updated_at_ns"] == (2 if not data["pull_requests"] else 1)
    offline = client.get(f"/v1/sessions/{session_id}/resources/github")
    assert offline.status_code == 200, offline.text
    assert offline.json()["reason"] == "workspace_offline"
    assert offline.json()["prs"] == data["pull_requests"]
    if data["pull_requests"]:
        assert offline.json()["cached_at"] == 3
        assert offline.json()["pr"]["body"] == "Saved description"
        assert offline.json()["pr"]["comments"][0]["body"] == "Saved comment"
        changes = client.get(f"/v1/sessions/{session_id}/resources/github/changes")
        assert changes.status_code == 200 and changes.json()["data"][0]["path"] == "test.py"
        diff = client.get(f"/v1/sessions/{session_id}/resources/github/diff")
        assert diff.status_code == 200 and diff.json()["patch"] == "Saved patch"
    print(json.dumps({"id": session_id, "todos": data["todos"], "items": data["items"],
                      "pull_requests": data["pull_requests"]}))
"""


def test_session_metadata_hydrates_after_a_real_server_process_restart(tmp_path) -> None:
    database = f"sqlite:///{tmp_path / 'restart.db'}"

    def run(mode: str, *args: str) -> dict:
        result = subprocess.check_output(
            [
                sys.executable,
                "-c",
                _SERVER_PROBE,
                database,
                str(tmp_path / "artifacts"),
                mode,
                *args,
            ],
            text=True,
            timeout=45,
        )
        return json.loads(result.strip().splitlines()[-1])

    before = run("report")
    assert run("read", before["id"]) == before and before["items"] == []
    assert before["todos"][0]["status"] == "in_progress"
    assert before["pull_requests"][0]["number"] == 42
    cleared = run("clear", before["id"])
    assert run("read", before["id"]) == cleared and cleared["todos"] == []
    assert cleared["pull_requests"] == []
