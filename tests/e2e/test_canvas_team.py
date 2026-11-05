"""A project's team across real processes: the boundary, worktrees and team updates."""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.timeout(200)
def test_lead_coordinates_inside_its_project(tmp_path, isolated_mock_llm_server_url):
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    base_env = {
        key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "TMPDIR"}
    }
    env = {
        "OMNIGENT_FEATURES": "canvas",
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    with server_runner(
        tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
    ) as stack:
        repo = Path(stack.workspace)
        for command in (
            ["init", "-q", "-b", "main"],
            ["config", "user.email", "team@example.test"],
            ["config", "user.name", "Team"],
            ["commit", "-q", "--allow-empty", "-m", "first"],
        ):
            subprocess.run(["git", "-C", str(repo), *command], check=True)
        stack.start_host(env=env, cwd=_REPO)
        with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
            poll_until(
                lambda: [
                    host
                    for host in client.get("/v1/hosts").json().get("hosts", [])
                    if host.get("status") == "online"
                ],
                timeout_s=45,
            )
            host_id = client.get("/v1/hosts").json()["hosts"][0]["host_id"]
            set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')

            def create_session(title, project_id):
                spec = {
                    "name": title.lower(),
                    "prompt": "Coordinate session work.",
                    "spawn": True,
                    "executor": {
                        "harness": "openai-agents",
                        "model": "gpt-4o",
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock}/v1",
                        },
                    },
                    "skills_filter": "none",
                }
                response = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
                    metadata={"title": title, "host_id": host_id, "workspace": str(repo)},
                )
                response.raise_for_status()
                session_id = response.json()["session_id"]
                filed = client.patch(f"/v1/sessions/{session_id}", json={"project_id": project_id})
                assert filed.status_code == 200, filed.text
                return session_id

            def items(session_id):
                response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
                response.raise_for_status()
                return response.json()["data"]

            def tool_output(session_id, call_id):
                return next(
                    str(item.get("output"))
                    for item in items(session_id)
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == call_id
                )

            project_a = client.post("/v1/projects", json={"name": "Project A"}).json()["id"]
            project_b = client.post("/v1/projects", json={"name": "Project B"}).json()["id"]
            lead = create_session("Lead", project_a)
            teammate = create_session("Teammate", project_a)
            outsider = create_session("Outsider", project_b)
            agent_id = client.get(f"/v1/sessions/{teammate}").json()["agent_id"]

            def call(name, call_id, **arguments):
                return {
                    "tool_calls": [
                        {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}
                    ]
                }

            def configure(marker, responses):
                configure_mock_llm(
                    mock, responses, match=marker, required_tools=["sys_session_message"]
                )

            configure("TEAMMATE_TASK", [{"text": "TEAMMATE_RESULT"}])
            configure("teammate session", [{"text": "LEAD_WOKEN"}])
            configure(
                "LEAD_GO",
                [
                    call("sys_session_message", "outside", session_id=outsider, message="NOPE"),
                    call("sys_session_get_history", "history", conversation_id=outsider),
                    call(
                        "sys_session_create",
                        "created",
                        agent_id=agent_id,
                        placement="same_host",
                        title="Created worker",
                        branch_name="team/created",
                    ),
                    call("sys_session_list", "listed"),
                    call(
                        "sys_session_message",
                        "inside",
                        session_id=teammate,
                        message="TEAMMATE_TASK",
                    ),
                    {"text": "LEAD_SENT"},
                ],
            )
            response = client.post(
                f"/v1/sessions/{lead}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "LEAD_GO"}],
                    },
                },
            )
            assert response.status_code == 202, response.text

            poll_until(lambda: "LEAD_SENT" in json.dumps(items(lead)), timeout_s=90)
            assert "outside this session's project" in tool_output(lead, "outside")
            assert "session_out_of_tree" in tool_output(lead, "history")
            assert "NOPE" not in json.dumps(items(outsider))
            listed = tool_output(lead, "listed")
            assert teammate in listed and outsider not in listed

            created = next(
                row
                for row in client.get(
                    "/v1/sessions", params={"limit": 20, "visibility": "mine"}
                ).json()["data"]
                if row.get("title") == "Created worker"
            )
            assert created["project_id"] == project_a
            assert created["git_branch"] == "team/created"
            assert created["workspace"] != str(repo)
            worktrees = subprocess.run(
                ["git", "-C", str(repo), "worktree", "list"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            assert "[team/created]" in worktrees
            assert client.get(f"/v1/sessions/{lead}").json()["workspace"] == str(repo)

            # Nobody prompts the lead again: the finished teammate wakes it.
            poll_until(lambda: "LEAD_WOKEN" in json.dumps(items(lead)), timeout_s=60)
            assert teammate in json.dumps(items(lead))
