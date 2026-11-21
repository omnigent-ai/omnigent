"""A nested agent works in the permission mode of the native session that started it.

Real local server, host and native CLI processes; scripted model, no cloud sandbox.
"""

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    PI_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle, send_user_text
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_OPENAI_MODEL = "gpt-4o"
_ANTHROPIC_MODEL = "claude-sonnet-4-5"
# Each later marker extends the earlier one, so the mock routes a resent
# conversation to its newest turn.
_RUN_TASK = "NESTED_MODE_RUN"
_WORKER_REPORT = f"{_RUN_TASK}_WORKER_REPORT"
_WORKER_TASK = "NESTED_MODE_WORKER_TASK"


@dataclass(frozen=True)
class _Harness:
    """How a native CLI runs the scenario.

    :param launch_args: The run's permission mode, as the Run dialog sends it.
    :param mode_label: The label where the CLI reports the mode it runs in.
    :param run_mode: The mode the run reports there.
    :param ready_token: Text in a model request once the Omnigent tools are listed.
    :param tool_guard: A tool every scripted request must advertise.
    """

    launch_args: list[str] | None
    mode_label: str | None
    run_mode: str | None
    ready_token: str
    tool_guard: str


_HARNESSES = {
    "codex": _Harness(
        PERMISSION_LAUNCH_ARGS["codex-native"]["full-access"],
        "omnigent.codex_native.approval_mode",
        "full-access",
        '"mcp__omnigent"',
        "mcp__omnigent",
    ),
    "claude": _Harness(
        PERMISSION_LAUNCH_ARGS["claude-native"]["bypassPermissions"],
        "omnigent.claude_native.permission_mode",
        "bypassPermissions",
        '"mcp__omnigent__sys_session_create"',
        "mcp__omnigent__sys_session_message",
    ),
    # Pi has no permission modes and asks no one, so it is a control.
    "pi": _Harness(None, None, None, '"sys_session_create"', "sys_session_message"),
}


def _user_text(request: dict) -> str:
    """The user-role text of a captured Responses or Messages request."""
    entries = [*request.get("input", []), *request.get("messages", [])]
    texts = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("role") == "user":
            content = entry.get("content")
            blocks = content if isinstance(content, list) else [content]
            texts.extend(
                block if isinstance(block, str) else str(block.get("text", ""))
                for block in blocks
                if isinstance(block, (str, dict))
            )
    return "\n".join(texts)


def _tool_call(harness: str, name: str, arguments: dict, call_id: str) -> dict:
    """A scripted model call of Omnigent tool *name*, named as *harness* lists it."""
    call = {"name": name, "arguments": json.dumps(arguments), "call_id": call_id}
    if harness == "codex":
        return {**call, "namespace": "mcp__omnigent"}
    if harness == "claude":
        return {**call, "name": f"mcp__omnigent__{name}"}
    return call


@pytest.mark.timeout(420)
@pytest.mark.parametrize("harness", list(_HARNESSES))
def test_nested_agent_runs_unattended_like_its_creator(
    tmp_path, isolated_mock_llm_server_url, harness
):
    # Each parameter is named for its CLI's binary.
    missing = cli_unavailable_reason(harness)
    if missing is not None:
        pytest.skip(missing)
    if shutil.which("tmux") is None:
        pytest.skip("native CLIs require tmux")
    spec = _HARNESSES[harness]
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "local": {
                        "kind": "key",
                        "default": ["openai", "anthropic", "pi"],
                        "openai": {
                            "base_url": f"{mock}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _OPENAI_MODEL},
                        },
                        "anthropic": {
                            "base_url": mock,
                            "api_key": "mock-key",
                            "models": {"default": _ANTHROPIC_MODEL},
                        },
                    }
                },
            }
        )
    )
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
            "PI_OFFLINE": "1",
            "TERM": "xterm-256color",
        }
        with server_runner(
            tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
        ) as stack:
            stack.start_host(env=env, cwd=_REPO)
            with httpx.Client(
                base_url=stack.base_url,
                timeout=60,
                trust_env=False,
                headers={"x-omnigent-background-session-titles": "off"},
            ) as client:
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

                def configure(marker, responses):
                    configure_mock_llm(
                        mock, responses, match=marker, required_tools=[spec.tool_guard]
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

                def requests_mentioning(text):
                    return [r for r in get_mock_requests(mock) if text in json.dumps(r)]

                def resend_until_tools_listed(session_id, marker, settled):
                    # A CLI can take a turn before its Omnigent tools are listed, and
                    # no queue serves that turn; ask again once the session is idle.
                    seen_requests = 0

                    def ready():
                        nonlocal seen_requests
                        if settled():
                            return True
                        requests = [r for r in get_mock_requests(mock) if marker in _user_text(r)]
                        if any(spec.ready_token in json.dumps(r) for r in requests):
                            return True
                        snapshot = session(session_id)
                        if snapshot["status"] != "idle" or snapshot.get("pending_inputs"):
                            return False
                        if len(requests) > seen_requests:
                            seen_requests = len(requests)
                            send_user_text(client, session_id=session_id, text=marker)
                        return False

                    poll_until(ready, timeout_s=120)

                # A saved workflow's run, launched in the mode picked in the Run dialog.
                metadata = {
                    "title": "Run Saved Workflow",
                    "host_id": host_id,
                    "workspace": str(stack.workspace),
                }
                if spec.launch_args is not None:
                    metadata["terminal_launch_args"] = spec.launch_args
                if harness == "pi":
                    pi_spec = yaml.safe_load(_materialize_pi_agent_spec(tmp_path).read_text())
                    pi_spec["executor"]["model"] = _ANTHROPIC_MODEL
                    pi_spec["executor"]["auth"] = {"type": "provider", "name": "local"}
                    metadata["labels"] = {
                        WRAPPER_LABEL_KEY: PI_NATIVE_WRAPPER_VALUE,
                        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                    }
                    created = post_session_bundle(
                        client.post,
                        "/v1/sessions",
                        bundle_files({"agent.yaml": yaml.safe_dump(pi_spec).encode()}),
                        metadata=metadata,
                    )
                    created.raise_for_status()
                    run = created.json()["session_id"]
                else:
                    run = create_native_session(
                        client,
                        stack.base_url,
                        harness=harness,
                        model=_OPENAI_MODEL if harness == "codex" else None,
                        metadata=metadata,
                    )["session_id"]
                probe = "NESTED_MODE_TOOLS_PROBE"
                send_user_text(client, session_id=run, text=probe)
                resend_until_tools_listed(run, probe, lambda: False)
                if spec.mode_label is not None:
                    # The CLI itself reports the run's mode.
                    poll_until(
                        lambda: session(run)["labels"].get(spec.mode_label) == spec.run_mode,
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
                                _tool_call(
                                    harness,
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
                                _tool_call(
                                    harness,
                                    "sys_session_message",
                                    {"session_id": run, "message": _WORKER_REPORT},
                                    "report",
                                )
                            ]
                        },
                        {"text": "NESTED_MODE_WORKER_DONE"},
                    ],
                )
                send_user_text(client, session_id=run, text=_RUN_TASK)
                poll_until(lambda: "NESTED_MODE_RUN_STARTED_WORKER" in items(run), timeout_s=120)
                # The run's own Omnigent call needed no approval.
                assert not session(run)["pending_elicitations"]
                rows = client.get("/v1/sessions", params={"visibility": "mine"}).json()["data"]
                workers = [row["id"] for row in rows if row["title"] == "Review step"]
                assert len(workers) == 1, requests_mentioning("create-worker")[-1:]
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
                poll_until(worker_settled, timeout_s=120)
                run_snapshot = session(run)
                observed = {
                    "approval_prompts": [
                        json.dumps(prompt)[:300] for prompt in latest["pending_elicitations"]
                    ],
                    "launch_args": latest["terminal_launch_args"],
                    "reported_mode": latest["labels"].get(spec.mode_label or ""),
                }
                assert observed == {
                    "approval_prompts": [],
                    "launch_args": run_snapshot["terminal_launch_args"],
                    "reported_mode": spec.run_mode,
                }
                # The worker's report reached the run.
                poll_until(lambda: "NESTED_MODE_RUN_GOT_REPORT" in items(run), timeout_s=120)
