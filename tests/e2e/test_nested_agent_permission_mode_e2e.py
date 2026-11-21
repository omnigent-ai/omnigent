"""A nested agent works in the permission mode of the Codex session that started it.

Real local server, host and Codex processes; scripted model, no cloud sandbox.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from dev.repro_env.runtime import write_model_config
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_APPROVAL_MODE_LABEL_KEY = "omnigent.codex_native.approval_mode"
# Each later marker extends the earlier one, so the mock routes a resent
# conversation to its newest turn.
_RUN_TASK = "NESTED_MODE_RUN"
_WORKER_REPORT = f"{_RUN_TASK}_WORKER_REPORT"
_WORKER_TASK = "NESTED_MODE_WORKER_TASK"


def _user_text(request: dict) -> str:
    """The user-role text of a captured Responses request, which routes it."""
    texts = []
    for entry in request.get("input", []):
        if isinstance(entry, dict) and entry.get("role") == "user":
            content = entry.get("content")
            blocks = content if isinstance(content, list) else [content]
            texts.extend(
                block if isinstance(block, str) else str(block.get("text", ""))
                for block in blocks
                if isinstance(block, (str, dict))
            )
    return "\n".join(texts)


@pytest.mark.timeout(300)
def test_nested_agent_runs_unattended_like_its_full_access_creator(
    tmp_path, isolated_mock_llm_server_url
):
    if any(shutil.which(cmd) is None for cmd in ("codex", "tmux")):
        pytest.skip("requires Codex and tmux")
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TMPDIR"}}
    with tempfile.TemporaryDirectory(prefix="nested-mode-", dir="/tmp") as native_state:
        env = {
            "OMNIGENT_FEATURES": "sibling_sessions",
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
        }
        with server_runner(
            tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
        ) as stack:
            stack.start_host(env=env, cwd=_REPO)
            with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
                poll_until(
                    lambda: [
                        h
                        for h in client.get("/v1/hosts").json().get("hosts", [])
                        if h.get("status") == "online"
                    ],
                    timeout_s=45,
                )
                host_id = client.get("/v1/hosts").json()["hosts"][0]["host_id"]
                set_fallback_mock_llm(
                    mock, key="_policy_llm_", text='{"action":"allow","reason":""}'
                )

                def tool(name, args, call_id):
                    return {
                        "name": name,
                        "arguments": json.dumps(args),
                        "call_id": call_id,
                        "namespace": "mcp__omnigent",
                    }

                def configure(marker, responses):
                    configure_mock_llm(
                        mock, responses, match=marker, required_tools=["mcp__omnigent"]
                    )

                def session(session_id):
                    response = client.get(f"/v1/sessions/{session_id}")
                    response.raise_for_status()
                    return response.json()

                def items(session_id):
                    response = client.get(
                        f"/v1/sessions/{session_id}/items", params={"limit": 100}
                    )
                    response.raise_for_status()
                    return json.dumps(response.json()["data"])

                def tool_outputs(call_id):
                    return [
                        entry.get("output")
                        for request in get_mock_requests(mock)
                        for entry in request.get("input", [])
                        if isinstance(entry, dict)
                        and entry.get("type") == "function_call_output"
                        and entry.get("call_id") == call_id
                    ]

                def send(session_id, text):
                    response = client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={
                            "type": "message",
                            "data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": text}],
                            },
                        },
                    )
                    assert response.status_code == 202, response.text

                def resend_until_tools_listed(session_id, marker, settled):
                    # Codex can take a turn before its omnigent MCP tools are listed, and
                    # no queue serves that turn; ask again once the session is idle.
                    seen_requests = 0

                    def ready():
                        nonlocal seen_requests
                        if settled():
                            return True
                        requests = [r for r in get_mock_requests(mock) if marker in _user_text(r)]
                        if any(
                            t.get("name") == "mcp__omnigent"
                            for r in requests
                            for t in r.get("tools", [])
                        ):
                            return True
                        snapshot = session(session_id)
                        if snapshot["status"] != "idle" or snapshot.get("pending_inputs"):
                            return False
                        if len(requests) > seen_requests:
                            seen_requests = len(requests)
                            send(session_id, marker)
                        return False

                    poll_until(ready, timeout_s=90)

                # A saved workflow's run, launched in the mode picked in the Run dialog.
                run = create_native_session(
                    client,
                    stack.base_url,
                    harness="codex",
                    model="gpt-4o",
                    metadata={
                        "title": "Run Saved Workflow",
                        "host_id": host_id,
                        "workspace": str(stack.workspace),
                        "terminal_launch_args": PERMISSION_LAUNCH_ARGS["codex-native"][
                            "full-access"
                        ],
                    },
                )["session_id"]
                probe = "NESTED_MODE_TOOLS_PROBE"
                send(run, probe)
                resend_until_tools_listed(run, probe, lambda: False)
                # Codex itself reports the run's mode.
                poll_until(
                    lambda: session(run)["labels"].get(_APPROVAL_MODE_LABEL_KEY) == "full-access",
                    timeout_s=30,
                )
                run_snapshot = session(run)

                # The run hands a step to a nested agent of its own agent without a
                # permission_mode, as the workflow skill does; the worker reports back.
                configure(
                    _RUN_TASK,
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_create",
                                    {
                                        "agent_id": run_snapshot["agent_id"],
                                        "placement": "same_host",
                                        "title": "Review step",
                                        "message": _WORKER_TASK,
                                    },
                                    "create-worker",
                                )
                            ]
                        },
                        {"text": "NESTED_MODE_RUN_STARTED_WORKER"},
                    ],
                )
                configure(_WORKER_REPORT, [{"text": "NESTED_MODE_RUN_GOT_REPORT"}])
                configure(
                    _WORKER_TASK,
                    [
                        {
                            "tool_calls": [
                                tool(
                                    "sys_session_message",
                                    {"session_id": run, "message": _WORKER_REPORT},
                                    "report",
                                )
                            ]
                        },
                        {"text": "NESTED_MODE_WORKER_DONE"},
                    ],
                )
                send(run, _RUN_TASK)
                poll_until(lambda: "NESTED_MODE_RUN_STARTED_WORKER" in items(run), timeout_s=90)
                # The run's own Omnigent call needed no approval.
                assert not session(run)["pending_elicitations"]
                rows = client.get("/v1/sessions", params={"visibility": "mine"}).json()["data"]
                workers = [row["id"] for row in rows if row["title"] == "Review step"]
                assert len(workers) == 1, tool_outputs("create-worker")
                (worker,) = workers
                assert session(worker)["labels"]["omnigent.created_by_session_id"] == run

                # The worker either finishes its Omnigent call or stops to ask for approval.
                latest = {}

                def worker_settled():
                    latest.update(session(worker))
                    return bool(latest["pending_elicitations"]) or (
                        "NESTED_MODE_WORKER_DONE" in items(worker)
                    )

                resend_until_tools_listed(worker, _WORKER_TASK, worker_settled)
                poll_until(worker_settled, timeout_s=90)
                run_snapshot = session(run)
                observed = {
                    "approval_prompts": [
                        json.dumps(prompt)[:300] for prompt in latest["pending_elicitations"]
                    ],
                    "launch_args": latest["terminal_launch_args"],
                    "codex_approval_mode": latest["labels"].get(_APPROVAL_MODE_LABEL_KEY),
                }
                assert observed == {
                    "approval_prompts": [],
                    "launch_args": run_snapshot["terminal_launch_args"],
                    "codex_approval_mode": "full-access",
                }
                # The worker's report reached the run.
                poll_until(lambda: "NESTED_MODE_RUN_GOT_REPORT" in items(run), timeout_s=90)
