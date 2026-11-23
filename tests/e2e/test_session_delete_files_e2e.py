"""E2E: deleting a session removes the files of every session deleted with it.

Each journey runs a real server (and, when a model turn is needed, a real
runner and openai-agents harness) with its own database and artifact store.
Only the model endpoint is scripted. A deleted session's files are no longer
reachable through the API, so the tests read the server's file store and
artifact directory directly.

Run: uv run --no-sync pytest tests/e2e/test_session_delete_files_e2e.py -v
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
    set_fallback_mock_llm,
)

pytestmark = [pytest.mark.timeout(600, method="signal")]

_REPO = Path(__file__).resolve().parents[2]
_OS_ENV = {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}


@contextmanager
def _stack(root: Path, *, runner: bool) -> Iterator[tuple[httpx.Client, ServerRunner]]:
    """A fresh server, optionally with a runner, and a first-party client for it."""
    with server_runner(root, server_cwd=_REPO, poll_interval=0.2) as stack:
        if runner:
            stack.start_runner(cwd=_REPO)
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
        ) as client:
            yield client, stack


def _register_agent(client: httpx.Client, mock_llm_url: str, uid: str) -> str:
    """Register a minimal agent for sessions that run no turn."""
    return register_inline_agent(
        client,
        name=f"delete-files-{uid}",
        harness="openai-agents",
        model=f"mock-delete-parent-{uid}",
        profile="",
        prompt="Unused: these sessions run no turn.",
        mock_llm_base_url=f"{mock_llm_url}/v1",
    )


def _file_sharing_session(
    client: httpx.Client, mock_llm_url: str, uid: str, runner_id: str
) -> str:
    """
    A runner-bound session whose agent and ``worker`` sub-agent both turn the
    file tools on, each on its own mock model queue.

    An inline sub-agent can't set ``tool_groups``, so the worker is a bundled
    ``agents/worker`` agent.
    """
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_llm_url}/v1"}

    def agent_config(name: str, model: str, **extra: Any) -> bytes:
        config = {
            "spec_version": 1,
            "name": name,
            "prompt": "Follow the scripted mock tool calls exactly.",
            "executor": {
                "type": "omnigent",
                "model": model,
                "config": {"harness": "openai-agents"},
                "auth": auth,
            },
            "os_env": _OS_ENV,
            "tool_groups": {"files": True},
            **extra,
        }
        return yaml.safe_dump(config).encode()

    bundle = bundle_files(
        {
            "config.yaml": agent_config(
                f"delete-files-{uid}", f"mock-delete-parent-{uid}", tools={"agents": ["worker"]}
            ),
            "agents/worker/config.yaml": agent_config(
                "worker", f"mock-delete-child-{uid}", description="Uploads its notes."
            ),
        }
    )
    created = post_session_bundle(client.post, "/v1/sessions", bundle)
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    bind_session_runner(client.patch, "", session_id, runner_id)
    return session_id


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Script one mock model response that calls a single tool."""
    return {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _create(client: httpx.Client, **body: Any) -> str:
    response = client.post("/v1/sessions", json=body)
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _upload(client: httpx.Client, session_id: str, name: str, content: bytes) -> str:
    """Upload a file as the web composer and the runner's ``sys_files_upload`` do."""
    response = client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": (name, content, "text/plain")},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _listed_files(client: httpx.Client, session_id: str) -> dict[str, str]:
    """The session's files as the API lists them, by name."""
    response = client.get(f"/v1/sessions/{session_id}/resources/files")
    response.raise_for_status()
    return {entry["name"]: entry["id"] for entry in response.json()["data"]}


def _stored_files(stack: ServerRunner, *session_ids: str) -> dict[str, set[tuple[str, str]]]:
    """Each session's ``(filename, blob key)`` rows in the server's file store."""
    store = SqlAlchemyFileStore(stack.database_uri)
    return {
        session_id: {
            (f.filename, f.blob_key or f.id) for f in store.list(session_id, limit=100).data
        }
        for session_id in session_ids
    }


def _blobs_on_disk(stack: ServerRunner, blob_keys: set[str]) -> set[str]:
    return {key for key in blob_keys if (stack.artifact_location / key).exists()}


def _tool_outputs(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Each recorded tool call's output, by call id."""
    response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
    response.raise_for_status()
    return {
        item["call_id"]: str(item.get("output", ""))
        for item in response.json()["data"]
        if item.get("type") == "function_call_output"
    }


def _idle(client: httpx.Client, session_id: str) -> bool:
    return client.get(f"/v1/sessions/{session_id}").json().get("status") == "idle"


def test_deleting_a_parent_removes_its_sub_agents_files(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """
    A turn uploads a report and hands the user's brief to a sub-agent, whose
    turn uploads notes, so both sessions hold files: the parent's own uploads,
    the sub-agent's copy of the brief and its notes. Deleting the parent
    removes every one of those rows and blobs, except a blob a fork of the
    parent still uses: the fork's brief stays readable.
    """
    mock = isolated_mock_llm_server_url
    uid = uuid.uuid4().hex[:6]
    with _stack(tmp_path / "stack", runner=True) as (client, stack):
        parent_id = _file_sharing_session(client, mock, uid, stack.runner_id)
        brief_id = _upload(client, parent_id, "brief.txt", b"brief from the user")
        configure_mock_llm(
            mock,
            [
                _tool_call("write", "sys_os_write", {"path": "report.md", "content": "report"}),
                _tool_call("parent_upload", "sys_files_upload", {"path": "report.md"}),
                _tool_call(
                    "dispatch",
                    "sys_session_send",
                    {
                        "agent": "worker",
                        "title": "notes",
                        "args": {"input": "Upload your notes.", "file_ids": [brief_id]},
                    },
                ),
                {"text": "Dispatched."},
            ],
            key=f"mock-delete-parent-{uid}",
        )
        # The worker's handoff wakes the parent again; that turn just finishes.
        set_fallback_mock_llm(mock, key=f"mock-delete-parent-{uid}", text="Done.")
        configure_mock_llm(
            mock,
            [
                _tool_call("notes", "sys_os_write", {"path": "notes.md", "content": "notes"}),
                _tool_call("child_upload", "sys_files_upload", {"path": "notes.md"}),
                {"text": "Uploaded."},
            ],
            key=f"mock-delete-child-{uid}",
        )
        response_id = send_user_message_to_session(
            client,
            session_id=parent_id,
            content=[
                {
                    "type": "input_text",
                    "text": "Share a report, then give my brief to the worker.",
                },
                {"type": "input_file", "file_id": brief_id, "filename": "brief.txt"},
            ],
        )
        body = poll_session_until_terminal(
            client, session_id=parent_id, response_id=response_id, timeout=180
        )
        assert body["status"] == "completed", (body.get("status"), body.get("error"))
        children = client.get(f"/v1/sessions/{parent_id}/child_sessions").json()["data"]
        assert len(children) == 1, children
        child_id = children[0]["id"]
        poll_until(
            lambda: set(_listed_files(client, child_id)) == {"brief.txt", "notes.md"},
            timeout_s=120,
        )
        poll_until(lambda: _idle(client, parent_id) and _idle(client, child_id), timeout_s=120)
        outputs = {**_tool_outputs(client, parent_id), **_tool_outputs(client, child_id)}
        assert json.loads(outputs["parent_upload"])["filename"] == "report.md", outputs
        assert json.loads(outputs["child_upload"])["filename"] == "notes.md", outputs
        assert json.loads(outputs["dispatch"])["conversation_id"] == child_id, outputs

        fork = client.post(f"/v1/sessions/{parent_id}/fork", json={})
        assert fork.status_code == 201, fork.text
        fork_id = fork.json()["id"]
        fork_brief_id = _listed_files(client, fork_id)["brief.txt"]
        fork_blobs = {key for _, key in _stored_files(stack, fork_id)[fork_id]}
        before = _stored_files(stack, parent_id, child_id)
        assert {name for name, _ in before[parent_id]} == {"brief.txt", "report.md"}
        assert {name for name, _ in before[child_id]} == {"brief.txt", "notes.md"}
        blobs = {key for rows in before.values() for _, key in rows}
        assert _blobs_on_disk(stack, blobs) == blobs

        deleted = client.delete(f"/v1/sessions/{parent_id}")

        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/v1/sessions/{child_id}").status_code == 404
        assert _stored_files(stack, parent_id, child_id) == {parent_id: set(), child_id: set()}
        assert _blobs_on_disk(stack, blobs) == blobs & fork_blobs
        brief = client.get(f"/v1/sessions/{fork_id}/resources/files/{fork_brief_id}/content")
        assert brief.status_code == 200, brief.text
        assert brief.content == b"brief from the user"


def test_deleting_a_session_keeps_its_nested_agents_files(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """
    A nested agent is an independent session in its source's family, not a
    sub-agent, so deleting the source removes only the source's files.
    """
    uid = uuid.uuid4().hex[:6]
    with _stack(tmp_path / "stack", runner=False) as (client, stack):
        agent_id = lookup_agent_id(
            client, _register_agent(client, isolated_mock_llm_server_url, uid)
        )
        source_id = _create(client, agent_id=agent_id)
        nested_id = _create(client, agent_id=agent_id, sibling_source_id=source_id)
        listing = client.get(
            "/v1/sessions", params={"visibility": "all", "limit": 100, "expand_families": "true"}
        ).json()["data"]
        family = {row["id"]: row.get("family") or {} for row in listing}
        assert family[nested_id].get("representative_id") == source_id, family
        source_file = _upload(client, source_id, "source.txt", b"source file")
        nested_file = _upload(client, nested_id, "nested.txt", b"nested file")

        deleted = client.delete(f"/v1/sessions/{source_id}")

        assert deleted.status_code == 200, deleted.text
        assert client.get(f"/v1/sessions/{nested_id}").status_code == 200
        stored = _stored_files(stack, source_id, nested_id)
        assert stored == {source_id: set(), nested_id: {("nested.txt", nested_file)}}
        assert _blobs_on_disk(stack, {source_file, nested_file}) == {nested_file}
        content = client.get(f"/v1/sessions/{nested_id}/resources/files/{nested_file}/content")
        assert content.status_code == 200, content.text
        assert content.content == b"nested file"
